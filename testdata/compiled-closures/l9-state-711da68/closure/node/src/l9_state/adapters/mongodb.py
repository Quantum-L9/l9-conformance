from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar, cast

from pymongo import AsyncMongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.server_api import ServerApi
from pymongo.write_concern import WriteConcern

from ..errors import ContractRefused, StateConflict, StateError, StateInfrastructureFailure
from ..execution import ExecutionBudget
from ..internal_models import HardEraseReceipt
from ..lifecycle import build_restore_artifacts, build_tombstone_artifacts
from ..models import (
    StateAckReceipt,
    StateClaimReceipt,
    StateClaimRequest,
    StateReceipt,
    StateRef,
    StateReleaseRequest,
    StateRenewRequest,
    StateRestoreRequest,
    StateTombstoneRequest,
    StateTransitionEvent,
)
from ..outcomes import (
    claim_terminal_receipt,
    hard_erase_terminal_receipt,
    state_terminal_receipt,
    terminal_status,
)
from ..ports import (
    ACK_RECEIPT_SCHEMA_REF,
    CLAIM_RECEIPT_SCHEMA_REF,
    STATE_RECEIPT_SCHEMA_REF,
    EventSlice,
    HistorySlice,
    MutationReceipt,
    PendingRevision,
    StoredOperation,
    StoredRevision,
)
from .mongodb_contract import (
    COLLECTIONS,
    REQUIRED_INDEXES,
    acquire_claim_filter,
    acquire_claim_pipeline,
    claimless_transition_guard_filter,
    exact_claim_fence_filter,
    exact_claim_filter,
    operation_key,
    release_claim_pipeline,
    renew_claim_pipeline,
    state_key,
    transition_guard_pipeline,
)

R = TypeVar("R")
T = TypeVar("T", bound=StateReceipt | StateClaimReceipt | StateAckReceipt | HardEraseReceipt)


class MongoStateStore:
    """MongoDB StateStorePort using PyMongo Async and transaction-coupled State mechanics."""

    def __init__(self, client: AsyncMongoClient[dict[str, Any]], database_name: str) -> None:
        self._client = client
        self._db = client[database_name]
        self._objects = self._db[COLLECTIONS["objects"]]
        self._revisions = self._db[COLLECTIONS["revisions"]]
        self._operations = self._db[COLLECTIONS["operations"]]
        self._events = self._db[COLLECTIONS["events"]]
        self._claims = self._db[COLLECTIONS["claims"]]
        self._consumer_cursors = self._db[COLLECTIONS["consumer_cursors"]]
        self._scope_counters = self._db[COLLECTIONS["scope_counters"]]
        self._snapshot = ReadConcern("snapshot")
        self._majority_read = ReadConcern("majority")
        self._majority_write = WriteConcern("majority")

    @classmethod
    def from_uri(
        cls,
        uri: str,
        database_name: str,
        *,
        server_selection_timeout_ms: int = 5000,
    ) -> MongoStateStore:
        client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
            uri,
            server_api=ServerApi("1"),
            serverSelectionTimeoutMS=server_selection_timeout_ms,
            retryWrites=True,
        )
        return cls(client, database_name)

    async def close(self) -> None:
        await self._client.close()

    async def ensure_indexes(self) -> None:
        try:
            collections = {
                "objects": self._objects,
                "revisions": self._revisions,
                "operations": self._operations,
                "events": self._events,
                "claims": self._claims,
                "consumer_cursors": self._consumer_cursors,
                "scope_counters": self._scope_counters,
            }
            for collection_key, fields, unique, name in REQUIRED_INDEXES:
                await collections[collection_key].create_index(
                    list(fields), unique=unique, name=name
                )
        except PyMongoError as exc:
            raise StateInfrastructureFailure(
                code="STORAGE_UNAVAILABLE", message="could not ensure MongoDB indexes"
            ) from exc

    def _majority_collection(self, collection: Any) -> Any:
        return collection.with_options(
            read_concern=self._majority_read,
            read_preference=ReadPreference.PRIMARY,
        )

    @staticmethod
    def _revision_document(revision: StoredRevision) -> dict[str, Any]:
        return {
            "tenant_org_id": revision.tenant_org_id,
            "scope_ref": revision.state_ref.scope_ref,
            "object_id": revision.state_ref.object_id,
            "revision": revision.state_ref.revision,
            "state_ref": revision.state_ref.model_dump(mode="python"),
            "payload": revision.payload,
            "recorded_at": revision.recorded_at,
            "retention_policy_ref": revision.retention_policy_ref,
            "retention_policy_digest": revision.retention_policy_digest,
        }

    @staticmethod
    def _current_document(revision: StoredRevision) -> dict[str, Any]:
        return {
            "tenant_org_id": revision.tenant_org_id,
            "scope_ref": revision.state_ref.scope_ref,
            "object_id": revision.state_ref.object_id,
            "current_revision": revision.state_ref.revision,
            "state_ref": revision.state_ref.model_dump(mode="python"),
            "state_digest": revision.state_ref.state_digest,
            "schema_ref": revision.state_ref.schema_ref,
            "schema_digest": revision.state_ref.schema_digest,
            "lifecycle": revision.state_ref.lifecycle,
            "retention_policy_ref": revision.retention_policy_ref,
            "retention_policy_digest": revision.retention_policy_digest,
        }

    @staticmethod
    def _operation_document(
        tenant_org_id: str, scope_ref: str, operation: StoredOperation
    ) -> dict[str, Any]:
        return {
            "tenant_org_id": tenant_org_id,
            "scope_ref": scope_ref,
            "operation_id": operation.receipt.operation_id,
            "action": operation.action,
            "request_digest": operation.request_digest,
            "receipt_schema_ref": operation.receipt_schema_ref,
            "receipt": operation.receipt.model_dump(mode="python", exclude_none=True),
        }

    @staticmethod
    def _event_document(tenant_org_id: str, event: StateTransitionEvent) -> dict[str, Any]:
        return {
            "tenant_org_id": tenant_org_id,
            "scope_ref": event.state_ref.scope_ref,
            "object_id": event.state_ref.object_id,
            **event.model_dump(mode="python", exclude_none=True),
        }

    @staticmethod
    def _revision_from_document(doc: dict[str, Any]) -> StoredRevision:
        from ..models import StateRef

        return StoredRevision(
            tenant_org_id=str(doc["tenant_org_id"]),
            state_ref=StateRef.model_validate(doc["state_ref"]),
            payload=doc.get("payload"),
            recorded_at=doc["recorded_at"],
            retention_policy_ref=str(doc["retention_policy_ref"]),
            retention_policy_digest=str(doc["retention_policy_digest"]),
        )

    @staticmethod
    def _operation_from_document(doc: dict[str, Any]) -> StoredOperation:
        action = str(doc["action"])
        receipt_payload = dict(doc["receipt"])
        if action in {"state.claim", "state.renew", "state.release"}:
            receipt: MutationReceipt = StateClaimReceipt.model_validate(receipt_payload)
        elif action == "state.ack":
            receipt = StateAckReceipt.model_validate(receipt_payload)
        elif action == "state.internal.hard_erase":
            receipt = HardEraseReceipt.model_validate(receipt_payload)
        else:
            receipt = StateReceipt.model_validate(receipt_payload)
        return StoredOperation(
            action=action,
            request_digest=str(doc["request_digest"]),
            receipt_schema_ref=str(doc["receipt_schema_ref"]),
            receipt=receipt,
        )

    async def _get_operation_raw(
        self, tenant_org_id: str, scope_ref: str, operation_id: str, *, session: Any = None
    ) -> StoredOperation | None:
        collection = (
            self._majority_collection(self._operations) if session is None else self._operations
        )
        doc = await collection.find_one(
            operation_key(tenant_org_id, scope_ref, operation_id), session=session
        )
        return None if doc is None else self._operation_from_document(doc)

    async def _bounded_read(
        self,
        budget: ExecutionBudget | None,
        read: Coroutine[Any, Any, R],
        *,
        scope_ref: str,
        operation_id: str | None = None,
    ) -> R:
        """Bound a provider read by the request budget (decisions C11b and F-03).

        Driver server selection and read retries may only happen inside this deadline. A
        timeout is DEADLINE_EXCEEDED; nothing was mutated, so the caller's effect decides the
        retry class (read: later; pre-mutation lookup: same_operation).
        """
        if budget is None:
            return await read
        remaining_ms = budget.reconciliation_remaining_ms()
        try:
            if remaining_ms <= 0:
                read.close()
                raise TimeoutError("request budget exhausted before the read started")
            async with asyncio.timeout(remaining_ms / 1000.0):
                return await read
        except TimeoutError as exc:
            raise StateInfrastructureFailure(
                code="DEADLINE_EXCEEDED",
                message="trusted request budget expired during a storage read",
                operation_id=operation_id,
                scope_ref=scope_ref,
            ) from exc

    async def get_operation(
        self,
        tenant_org_id: str,
        scope_ref: str,
        operation_id: str,
        *,
        budget: ExecutionBudget | None = None,
    ) -> StoredOperation | None:
        try:
            return await self._bounded_read(
                budget,
                self._get_operation_raw(tenant_org_id, scope_ref, operation_id),
                scope_ref=scope_ref,
                operation_id=operation_id,
            )
        except PyMongoError as exc:
            raise StateInfrastructureFailure(
                code="STORAGE_UNAVAILABLE",
                message="operation lookup unavailable",
                operation_id=operation_id,
                scope_ref=scope_ref,
            ) from exc

    async def _get_revision_unbounded(
        self, tenant_org_id: str, scope_ref: str, object_id: str, revision: int | None = None
    ) -> StoredRevision | None:
        try:
            resolved = revision
            if resolved is None:
                current = await self._majority_collection(self._objects).find_one(
                    state_key(tenant_org_id, scope_ref, object_id),
                    projection={"current_revision": 1},
                )
                if current is None:
                    return None
                resolved = int(current["current_revision"])
            doc = await self._majority_collection(self._revisions).find_one(
                {
                    **state_key(tenant_org_id, scope_ref, object_id),
                    "revision": resolved,
                }
            )
            return None if doc is None else self._revision_from_document(doc)
        except PyMongoError as exc:
            raise StateInfrastructureFailure(
                code="STORAGE_UNAVAILABLE",
                message="state revision lookup unavailable",
                scope_ref=scope_ref,
            ) from exc

    @staticmethod
    def _resolve_existing(
        existing: StoredOperation | None, *, action: str, request_digest: str
    ) -> MutationReceipt | None:
        if existing is None:
            return None
        if existing.action == action and existing.request_digest == request_digest:
            return existing.receipt
        raise StateConflict(
            "IDEMPOTENCY_COLLISION", "operation_id already binds different semantics"
        )

    async def _next_journal_seq(self, tenant_org_id: str, scope_ref: str, session: Any) -> int:
        counter = await self._scope_counters.find_one_and_update(
            {"tenant_org_id": tenant_org_id, "scope_ref": scope_ref},
            {"$inc": {"next_journal_seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if counter is None:
            raise StateInfrastructureFailure(
                code="STORAGE_UNAVAILABLE", message="journal sequence allocation failed"
            )
        return int(counter["next_journal_seq"])

    async def _classify_claim_failure(
        self,
        *,
        tenant_org_id: str,
        scope_ref: str,
        object_id: str,
        session: Any,
        claim_id: str | None = None,
        fencing_token: int | None = None,
        holder_ref: str | None = None,
        claimless: bool = False,
    ) -> None:
        key = state_key(tenant_org_id, scope_ref, object_id)
        doc = await self._claims.find_one(key, session=session)
        if doc is None:
            raise StateConflict("CLAIM_NOT_FOUND", "claim lineage missing for StateKey")
        if claimless:
            active = await self._claims.find_one(
                {
                    **key,
                    "$expr": {
                        "$and": [
                            {"$eq": ["$active", True]},
                            {"$gt": ["$expires_at", "$$NOW"]},
                        ]
                    },
                },
                session=session,
            )
            if active is not None:
                raise StateConflict(
                    "CLAIM_REQUIRED", "an active claim requires exact claim and fence"
                )
            raise StateConflict("CLAIM_STALE_FENCE", "claim guard changed concurrently")
        if doc.get("claim_id") is None:
            raise StateConflict("CLAIM_NOT_FOUND", "no claim exists for StateKey")
        if claim_id is not None and (
            doc.get("claim_id") != claim_id or doc.get("fencing_token") != fencing_token
        ):
            raise StateConflict("CLAIM_STALE_FENCE", "claim identity or fence is stale")
        if holder_ref is not None and doc.get("holder_ref") != holder_ref:
            raise ContractRefused("CLAIM_HOLDER_MISMATCH", "claim belongs to another holder")
        if not bool(doc.get("active")):
            raise StateConflict("CLAIM_NOT_FOUND", "claim has been released")
        expired = await self._claims.find_one(
            {
                **key,
                "$expr": {"$lte": ["$expires_at", "$$NOW"]},
            },
            session=session,
        )
        if expired is not None:
            raise StateConflict("CLAIM_EXPIRED", "claim has expired")
        raise StateConflict("CLAIM_STALE_FENCE", "claim guard changed concurrently")

    async def _guard_transition_claim(
        self,
        *,
        tenant_org_id: str,
        scope_ref: str,
        object_id: str,
        claim_id: str | None,
        fencing_token: int | None,
        holder_ref: str,
        session: Any,
    ) -> None:
        if claim_id is None:
            filter_doc = claimless_transition_guard_filter(tenant_org_id, scope_ref, object_id)
        else:
            assert fencing_token is not None
            filter_doc = exact_claim_filter(
                tenant_org_id,
                scope_ref,
                object_id,
                claim_id=claim_id,
                fencing_token=fencing_token,
                holder_ref=holder_ref,
            )
        guarded = await self._claims.find_one_and_update(
            filter_doc,
            transition_guard_pipeline(),
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if guarded is None:
            await self._classify_claim_failure(
                tenant_org_id=tenant_org_id,
                scope_ref=scope_ref,
                object_id=object_id,
                claim_id=claim_id,
                fencing_token=fencing_token,
                holder_ref=holder_ref,
                claimless=claim_id is None,
                session=session,
            )

    async def _guard_hard_erase_claim(
        self,
        *,
        tenant_org_id: str,
        scope_ref: str,
        object_id: str,
        claim_id: str | None,
        fencing_token: int | None,
        session: Any,
    ) -> None:
        """Fence retention-authorized erase by claim lineage, not claim-holder identity."""
        if claim_id is None:
            filter_doc = claimless_transition_guard_filter(tenant_org_id, scope_ref, object_id)
        else:
            assert fencing_token is not None
            filter_doc = exact_claim_fence_filter(
                tenant_org_id,
                scope_ref,
                object_id,
                claim_id=claim_id,
                fencing_token=fencing_token,
            )
        guarded = await self._claims.find_one_and_update(
            filter_doc,
            transition_guard_pipeline(),
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if guarded is None:
            await self._classify_claim_failure(
                tenant_org_id=tenant_org_id,
                scope_ref=scope_ref,
                object_id=object_id,
                claim_id=claim_id,
                fencing_token=fencing_token,
                holder_ref=None,
                claimless=claim_id is None,
                session=session,
            )

    @staticmethod
    def _has_error_label(exc: PyMongoError, label: str) -> bool:
        has_label = getattr(exc, "has_error_label", None)
        return bool(callable(has_label) and has_label(label))

    @classmethod
    def _is_ambiguous_commit(cls, exc: PyMongoError) -> bool:
        return cls._has_error_label(exc, "UnknownTransactionCommitResult")

    @classmethod
    def _is_transient_transaction(cls, exc: PyMongoError) -> bool:
        return cls._has_error_label(exc, "TransientTransactionError")

    @staticmethod
    async def _abort_quietly(session: Any, budget: ExecutionBudget) -> None:
        """Best-effort abort, bounded by the request budget.

        Abort is cleanup, so it may spend the reconciliation reserve but never the return
        reserve. A stalled or skipped abort is safe for correctness: uncommitted transaction
        writes are never visible, the server discards the transaction at its lifetime limit,
        and pymongo marks the session's transaction ABORTED in a ``finally`` even when the
        abort is cancelled, so the session is neither re-aborted on end nor blocked for retry.
        """
        if not getattr(session, "in_transaction", False):
            return
        abort_ms = budget.reconciliation_remaining_ms()
        if abort_ms <= 0:
            return
        try:
            async with asyncio.timeout(abort_ms / 1000.0):
                await session.abort_transaction()
        except (TimeoutError, PyMongoError):
            return

    async def _reconcile_after_commit(
        self,
        *,
        tenant_org_id: str,
        scope_ref: str,
        operation_id: str,
        action: str,
        request_digest: str,
        budget: ExecutionBudget,
        cause: BaseException,
    ) -> T:
        last_error: BaseException = cause
        while budget.reconciliation_remaining_ms() > 0:
            remaining_ms = budget.reconciliation_remaining_ms()
            try:
                async with asyncio.timeout(max(0.001, remaining_ms / 1000.0)):
                    reconciled = await self._get_operation_raw(
                        tenant_org_id, scope_ref, operation_id
                    )
            except (TimeoutError, PyMongoError) as exc:
                last_error = exc
            else:
                resolved = self._resolve_existing(
                    reconciled, action=action, request_digest=request_digest
                )
                if resolved is not None:
                    return cast(T, resolved)

            remaining_ms = budget.reconciliation_remaining_ms()
            if remaining_ms <= 0:
                break
            sleep_ms = min(budget.policy.reconciliation_poll_ms, remaining_ms)
            await asyncio.sleep(sleep_ms / 1000.0)

        raise StateInfrastructureFailure(
            code="OUTCOME_UNKNOWN",
            message="commit may have completed but no authoritative receipt was observed within budget",
            operation_id=operation_id,
            scope_ref=scope_ref,
        ) from last_error

    async def _persist_terminal_receipt(
        self,
        *,
        tenant_org_id: str,
        scope_ref: str,
        operation_id: str,
        action: str,
        request_digest: str,
        receipt_schema_ref: str,
        receipt: T,
        budget: ExecutionBudget,
    ) -> T:
        operation = StoredOperation(action, request_digest, receipt_schema_ref, receipt)

        async def callback(session: Any) -> T:
            existing = await self._get_operation_raw(
                tenant_org_id, scope_ref, operation_id, session=session
            )
            resolved = self._resolve_existing(
                existing, action=action, request_digest=request_digest
            )
            if resolved is not None:
                # A concurrent attempt already recorded this OperationKey: its receipt wins.
                return cast(T, resolved)
            await self._operations.insert_one(
                self._operation_document(tenant_org_id, scope_ref, operation), session=session
            )
            return receipt

        return await self._run_mutation(
            tenant_org_id=tenant_org_id,
            scope_ref=scope_ref,
            operation_id=operation_id,
            action=action,
            request_digest=request_digest,
            receipt_schema_ref=receipt_schema_ref,
            callback=callback,
            logical_receipt_factory=None,
            budget=budget,
        )

    async def _run_mutation(
        self,
        *,
        tenant_org_id: str,
        scope_ref: str,
        operation_id: str,
        action: str,
        request_digest: str,
        receipt_schema_ref: str,
        callback: Callable[[Any], Awaitable[T]],
        logical_receipt_factory: Callable[[StateError], T] | None,
        budget: ExecutionBudget,
    ) -> T:
        budget.require_mutation_start(operation_id=operation_id, scope_ref=scope_ref)

        async with self._client.start_session() as session:
            while True:
                budget.require_mutation_start(operation_id=operation_id, scope_ref=scope_ref)
                max_commit_time_ms = max(1, budget.mutation_remaining_ms())
                try:
                    # C11b: transaction start is bounded by the mutation budget.
                    async with asyncio.timeout(max_commit_time_ms / 1000.0):
                        await session.start_transaction(
                            read_concern=self._snapshot,
                            write_concern=self._majority_write,
                            read_preference=ReadPreference.PRIMARY,
                            max_commit_time_ms=max_commit_time_ms,
                        )
                except TimeoutError as exc:
                    raise StateInfrastructureFailure(
                        code="DEADLINE_EXCEEDED",
                        message="trusted request budget expired before the transaction started",
                        operation_id=operation_id,
                        scope_ref=scope_ref,
                    ) from exc
                except PyMongoError as exc:
                    if self._is_transient_transaction(exc) and budget.can_start_mutation():
                        continue
                    raise StateInfrastructureFailure(
                        code="STORAGE_UNAVAILABLE",
                        message="MongoDB transaction could not start",
                        operation_id=operation_id,
                        scope_ref=scope_ref,
                    ) from exc

                terminal_receipt: T | None = None
                try:
                    callback_ms = budget.mutation_remaining_ms()
                    if callback_ms <= 0:
                        raise TimeoutError("mutation budget exhausted before callback")
                    async with asyncio.timeout(callback_ms / 1000.0):
                        try:
                            result = await callback(session)
                        except (StateConflict, ContractRefused) as logical:
                            if (
                                logical.code == "IDEMPOTENCY_COLLISION"
                                or logical_receipt_factory is None
                            ):
                                raise
                            terminal_receipt = logical_receipt_factory(logical)
                except TimeoutError as exc:
                    await self._abort_quietly(session, budget)
                    raise StateInfrastructureFailure(
                        code="DEADLINE_EXCEEDED",
                        message="trusted request budget expired before commit was attempted",
                        operation_id=operation_id,
                        scope_ref=scope_ref,
                    ) from exc
                except (StateConflict, ContractRefused):
                    await self._abort_quietly(session, budget)
                    raise
                except StateInfrastructureFailure:
                    await self._abort_quietly(session, budget)
                    raise
                except PyMongoError as exc:
                    await self._abort_quietly(session, budget)
                    if (
                        self._is_transient_transaction(exc) or isinstance(exc, DuplicateKeyError)
                    ) and budget.can_start_mutation():
                        continue
                    raise StateInfrastructureFailure(
                        code="STORAGE_UNAVAILABLE",
                        message="MongoDB mutation failed before commit",
                        operation_id=operation_id,
                        scope_ref=scope_ref,
                    ) from exc

                if terminal_receipt is not None:
                    # C10 (receipt-only refusal unit): a negative terminal outcome never commits
                    # writes made by the effect callback (e.g. a claim-lineage guard write).
                    # Discard the effect transaction, then persist only the receipt in a fresh
                    # budgeted transaction that re-checks the OperationKey first.
                    await self._abort_quietly(session, budget)
                    return await self._persist_terminal_receipt(
                        tenant_org_id=tenant_org_id,
                        scope_ref=scope_ref,
                        operation_id=operation_id,
                        action=action,
                        request_digest=request_digest,
                        receipt_schema_ref=receipt_schema_ref,
                        receipt=terminal_receipt,
                        budget=budget,
                    )

                commit_ms = budget.mutation_remaining_ms()
                if commit_ms <= 0:
                    await self._abort_quietly(session, budget)
                    raise StateInfrastructureFailure(
                        code="DEADLINE_EXCEEDED",
                        message="trusted request budget expired before commit was attempted",
                        operation_id=operation_id,
                        scope_ref=scope_ref,
                    )

                try:
                    async with asyncio.timeout(commit_ms / 1000.0):
                        await session.commit_transaction()
                except TimeoutError as exc:
                    # A client-side timeout can race a successful server commit. Once commit
                    # may have crossed the line, semantic execution is forbidden; reconcile only.
                    return await self._reconcile_after_commit(
                        tenant_org_id=tenant_org_id,
                        scope_ref=scope_ref,
                        operation_id=operation_id,
                        action=action,
                        request_digest=request_digest,
                        budget=budget,
                        cause=exc,
                    )
                except PyMongoError as exc:
                    if self._is_ambiguous_commit(exc):
                        return await self._reconcile_after_commit(
                            tenant_org_id=tenant_org_id,
                            scope_ref=scope_ref,
                            operation_id=operation_id,
                            action=action,
                            request_digest=request_digest,
                            budget=budget,
                            cause=exc,
                        )
                    if self._is_transient_transaction(exc) and budget.can_start_mutation():
                        # Mongo labels this class as safe to retry as a whole transaction.
                        await self._abort_quietly(session, budget)
                        continue
                    await self._abort_quietly(session, budget)
                    raise StateInfrastructureFailure(
                        code="STORAGE_UNAVAILABLE",
                        message="MongoDB commit failed with a known non-ambiguous outcome",
                        operation_id=operation_id,
                        scope_ref=scope_ref,
                    ) from exc
                return result

    async def record_operation(
        self,
        *,
        tenant_org_id: str,
        scope_ref: str,
        operation: StoredOperation,
        budget: ExecutionBudget,
    ) -> MutationReceipt:
        operation_id = operation.receipt.operation_id

        async def callback(session: Any) -> MutationReceipt:
            existing = await self._get_operation_raw(
                tenant_org_id, scope_ref, operation_id, session=session
            )
            resolved = self._resolve_existing(
                existing, action=operation.action, request_digest=operation.request_digest
            )
            if resolved is not None:
                return resolved
            await self._operations.insert_one(
                self._operation_document(tenant_org_id, scope_ref, operation), session=session
            )
            return operation.receipt

        return await self._run_mutation(
            tenant_org_id=tenant_org_id,
            scope_ref=scope_ref,
            operation_id=operation_id,
            action=operation.action,
            request_digest=operation.request_digest,
            receipt_schema_ref=operation.receipt_schema_ref,
            callback=callback,
            logical_receipt_factory=None,
            budget=budget,
        )

    async def create(
        self,
        *,
        revision: StoredRevision,
        operation: StoredOperation,
        event: StateTransitionEvent,
        budget: ExecutionBudget,
    ) -> StateReceipt:
        tenant_org_id = revision.tenant_org_id
        scope_ref = revision.state_ref.scope_ref
        object_id = revision.state_ref.object_id
        operation_id = operation.receipt.operation_id

        async def callback(session: Any) -> StateReceipt:
            existing = await self._get_operation_raw(
                tenant_org_id, scope_ref, operation_id, session=session
            )
            resolved = self._resolve_existing(
                existing, action=operation.action, request_digest=operation.request_digest
            )
            if resolved is not None:
                if not isinstance(resolved, StateReceipt):
                    raise StateConflict(
                        "IDEMPOTENCY_COLLISION", "receipt family does not match action"
                    )
                return resolved
            key = state_key(tenant_org_id, scope_ref, object_id)
            existing_state = await self._objects.find_one(
                key, projection={"lifecycle": 1}, session=session
            )
            if existing_state is not None:
                if str(existing_state.get("lifecycle")) in {"tombstoned", "erased"}:
                    raise ContractRefused(
                        "OBJECT_ID_REUSE_FORBIDDEN",
                        "StateKey identity cannot be reused after lifecycle creation",
                    )
                raise StateConflict("OBJECT_ALREADY_EXISTS", "StateKey already exists")
            seq = await self._next_journal_seq(tenant_org_id, scope_ref, session)
            committed_event = event.model_copy(update={"journal_seq": seq})
            await self._revisions.insert_one(self._revision_document(revision), session=session)
            await self._objects.insert_one(self._current_document(revision), session=session)
            await self._claims.insert_one(
                {
                    **key,
                    "highest_fence": 0,
                    "active": False,
                    "claim_id": None,
                    "fencing_token": None,
                    "holder_ref": None,
                    "acquired_at": None,
                    "expires_at": None,
                    "released_at": None,
                    "guard_version": 0,
                },
                session=session,
            )
            await self._events.insert_one(
                self._event_document(tenant_org_id, committed_event), session=session
            )
            await self._operations.insert_one(
                self._operation_document(tenant_org_id, scope_ref, operation), session=session
            )
            if not isinstance(operation.receipt, StateReceipt):
                raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")
            return operation.receipt

        if not isinstance(operation.receipt, StateReceipt):
            raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")
        state_receipt = operation.receipt
        return await self._run_mutation(
            tenant_org_id=tenant_org_id,
            scope_ref=scope_ref,
            operation_id=operation_id,
            action=operation.action,
            request_digest=operation.request_digest,
            receipt_schema_ref=operation.receipt_schema_ref,
            callback=callback,
            logical_receipt_factory=lambda exc: state_terminal_receipt(state_receipt, exc),
            budget=budget,
        )

    async def transition(
        self,
        *,
        revision: PendingRevision,
        expected_revision: int,
        expected_state_digest: str,
        expected_schema_ref: str,
        expected_schema_digest: str,
        claim_id: str | None,
        fencing_token: int | None,
        holder_ref: str,
        operation: StoredOperation,
        event: StateTransitionEvent,
        budget: ExecutionBudget,
    ) -> StateReceipt:
        tenant_org_id = revision.tenant_org_id
        scope_ref = revision.state_ref.scope_ref
        object_id = revision.state_ref.object_id
        operation_id = operation.receipt.operation_id

        async def callback(session: Any) -> StateReceipt:
            existing = await self._get_operation_raw(
                tenant_org_id, scope_ref, operation_id, session=session
            )
            resolved = self._resolve_existing(
                existing, action=operation.action, request_digest=operation.request_digest
            )
            if resolved is not None:
                if not isinstance(resolved, StateReceipt):
                    raise StateConflict(
                        "IDEMPOTENCY_COLLISION", "receipt family does not match action"
                    )
                return resolved
            key = state_key(tenant_org_id, scope_ref, object_id)
            current = await self._objects.find_one(key, session=session)
            if current is None:
                raise StateConflict("OBJECT_NOT_FOUND", "StateKey does not exist")
            if str(current.get("lifecycle")) == "tombstoned":
                raise ContractRefused(
                    "OBJECT_TOMBSTONED", "ordinary transition requires active state"
                )
            if str(current.get("lifecycle")) == "erased":
                raise ContractRefused("OBJECT_ERASED", "erased state is terminal in v1")
            if int(current["current_revision"]) != expected_revision:
                raise StateConflict(
                    "REVISION_CONFLICT", "expected revision does not match current revision"
                )
            if str(current["state_digest"]) != expected_state_digest:
                raise StateConflict(
                    "STATE_DIGEST_CONFLICT", "expected state digest does not match current state"
                )
            if str(current["schema_ref"]) != expected_schema_ref:
                raise ContractRefused(
                    "SCHEMA_BINDING_IMMUTABLE", "expected schema ref does not match object binding"
                )
            if str(current["schema_digest"]) != expected_schema_digest:
                raise ContractRefused(
                    "SCHEMA_DIGEST_MISMATCH", "expected schema digest does not match object binding"
                )
            await self._guard_transition_claim(
                tenant_org_id=tenant_org_id,
                scope_ref=scope_ref,
                object_id=object_id,
                claim_id=claim_id,
                fencing_token=fencing_token,
                holder_ref=holder_ref,
                session=session,
            )
            final_revision = StoredRevision(
                tenant_org_id=tenant_org_id,
                state_ref=revision.state_ref,
                payload=revision.payload,
                recorded_at=revision.recorded_at,
                retention_policy_ref=str(current["retention_policy_ref"]),
                retention_policy_digest=str(current["retention_policy_digest"]),
            )
            updated = await self._objects.update_one(
                {
                    **key,
                    "current_revision": expected_revision,
                    "state_digest": expected_state_digest,
                    "schema_ref": expected_schema_ref,
                    "schema_digest": expected_schema_digest,
                },
                {"$set": self._current_document(final_revision)},
                session=session,
            )
            if updated.matched_count != 1:
                raise StateConflict(
                    "REVISION_CONFLICT", "state changed during conditional mutation"
                )
            seq = await self._next_journal_seq(tenant_org_id, scope_ref, session)
            committed_event = event.model_copy(update={"journal_seq": seq})
            await self._revisions.insert_one(
                self._revision_document(final_revision), session=session
            )
            await self._events.insert_one(
                self._event_document(tenant_org_id, committed_event), session=session
            )
            await self._operations.insert_one(
                self._operation_document(tenant_org_id, scope_ref, operation), session=session
            )
            if not isinstance(operation.receipt, StateReceipt):
                raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")
            return operation.receipt

        if not isinstance(operation.receipt, StateReceipt):
            raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")
        state_receipt = operation.receipt
        return await self._run_mutation(
            tenant_org_id=tenant_org_id,
            scope_ref=scope_ref,
            operation_id=operation_id,
            action=operation.action,
            request_digest=operation.request_digest,
            receipt_schema_ref=operation.receipt_schema_ref,
            callback=callback,
            logical_receipt_factory=lambda exc: state_terminal_receipt(state_receipt, exc),
            budget=budget,
        )

    async def tombstone(
        self,
        *,
        tenant_org_id: str,
        request: StateTombstoneRequest,
        request_digest: str,
        receipt_id: str,
        event_id: str,
        principal_ref: str,
        authorization_ref: str,
        causation_ref: str | None,
        budget: ExecutionBudget,
    ) -> StateReceipt:
        action: Literal["state.tombstone"] = "state.tombstone"
        logical_issued_at = datetime.now(UTC)

        def logical_receipt(exc: StateError) -> StateReceipt:
            return StateReceipt(
                contract_version="1.0",
                receipt_id=receipt_id,
                action=action,
                operation_id=request.operation_id,
                request_digest=request_digest,
                scope_ref=request.scope_ref,
                principal_ref=principal_ref,
                authorization_ref=authorization_ref,
                status=terminal_status(exc),
                issued_at=logical_issued_at,
                reason_codes=(exc.code,),
            )

        async def callback(session: Any) -> StateReceipt:
            existing = await self._get_operation_raw(
                tenant_org_id, request.scope_ref, request.operation_id, session=session
            )
            resolved = self._resolve_existing(
                existing, action=action, request_digest=request_digest
            )
            if resolved is not None:
                if not isinstance(resolved, StateReceipt):
                    raise StateConflict(
                        "IDEMPOTENCY_COLLISION", "receipt family does not match action"
                    )
                return resolved
            key = state_key(tenant_org_id, request.scope_ref, request.object_id)
            current_doc = await self._objects.find_one(key, session=session)
            if current_doc is None:
                raise StateConflict("OBJECT_NOT_FOUND", "StateKey does not exist")
            lifecycle = str(current_doc.get("lifecycle"))
            if lifecycle == "tombstoned":
                raise ContractRefused("OBJECT_TOMBSTONED", "object is already tombstoned")
            if lifecycle == "erased":
                raise ContractRefused("OBJECT_ERASED", "erased state is terminal in v1")
            if int(current_doc["current_revision"]) != request.expected_revision:
                raise StateConflict(
                    "REVISION_CONFLICT", "expected revision does not match current revision"
                )
            if str(current_doc["state_digest"]) != request.expected_state_digest:
                raise StateConflict(
                    "STATE_DIGEST_CONFLICT", "expected state digest does not match current state"
                )
            await self._guard_transition_claim(
                tenant_org_id=tenant_org_id,
                scope_ref=request.scope_ref,
                object_id=request.object_id,
                claim_id=request.claim_id,
                fencing_token=request.fencing_token,
                holder_ref=principal_ref,
                session=session,
            )
            revision_doc = await self._revisions.find_one(
                {**key, "revision": int(current_doc["current_revision"])}, session=session
            )
            if revision_doc is None:
                raise StateInfrastructureFailure(
                    code="STORAGE_UNAVAILABLE",
                    message="current immutable revision is missing",
                    operation_id=request.operation_id,
                    scope_ref=request.scope_ref,
                )
            current = self._revision_from_document(revision_doc)
            pending, receipt, event = build_tombstone_artifacts(
                current=current,
                request=request,
                request_digest=request_digest,
                receipt_id=receipt_id,
                event_id=event_id,
                principal_ref=principal_ref,
                authorization_ref=authorization_ref,
                committed_at=logical_issued_at,
                causation_ref=causation_ref,
            )
            final_revision = StoredRevision(
                tenant_org_id=tenant_org_id,
                state_ref=pending.state_ref,
                payload=pending.payload,
                recorded_at=pending.recorded_at,
                retention_policy_ref=current.retention_policy_ref,
                retention_policy_digest=current.retention_policy_digest,
            )
            updated = await self._objects.update_one(
                {
                    **key,
                    "current_revision": request.expected_revision,
                    "state_digest": request.expected_state_digest,
                    "lifecycle": "active",
                },
                {"$set": self._current_document(final_revision)},
                session=session,
            )
            if updated.matched_count != 1:
                raise StateConflict("REVISION_CONFLICT", "state changed during tombstone")
            seq = await self._next_journal_seq(tenant_org_id, request.scope_ref, session)
            committed_event = event.model_copy(update={"journal_seq": seq})
            await self._revisions.insert_one(
                self._revision_document(final_revision), session=session
            )
            await self._events.insert_one(
                self._event_document(tenant_org_id, committed_event), session=session
            )
            operation = StoredOperation(action, request_digest, STATE_RECEIPT_SCHEMA_REF, receipt)
            await self._operations.insert_one(
                self._operation_document(tenant_org_id, request.scope_ref, operation),
                session=session,
            )
            return receipt

        return await self._run_mutation(
            tenant_org_id=tenant_org_id,
            scope_ref=request.scope_ref,
            operation_id=request.operation_id,
            action=action,
            request_digest=request_digest,
            receipt_schema_ref=STATE_RECEIPT_SCHEMA_REF,
            callback=callback,
            logical_receipt_factory=logical_receipt,
            budget=budget,
        )

    async def restore(
        self,
        *,
        tenant_org_id: str,
        request: StateRestoreRequest,
        request_digest: str,
        receipt_id: str,
        event_id: str,
        principal_ref: str,
        authorization_ref: str,
        causation_ref: str | None,
        budget: ExecutionBudget,
    ) -> StateReceipt:
        action: Literal["state.restore"] = "state.restore"
        logical_issued_at = datetime.now(UTC)

        def logical_receipt(exc: StateError) -> StateReceipt:
            return StateReceipt(
                contract_version="1.0",
                receipt_id=receipt_id,
                action=action,
                operation_id=request.operation_id,
                request_digest=request_digest,
                scope_ref=request.scope_ref,
                principal_ref=principal_ref,
                authorization_ref=authorization_ref,
                status=terminal_status(exc),
                issued_at=logical_issued_at,
                reason_codes=(exc.code,),
            )

        async def callback(session: Any) -> StateReceipt:
            existing = await self._get_operation_raw(
                tenant_org_id, request.scope_ref, request.operation_id, session=session
            )
            resolved = self._resolve_existing(
                existing, action=action, request_digest=request_digest
            )
            if resolved is not None:
                if not isinstance(resolved, StateReceipt):
                    raise StateConflict(
                        "IDEMPOTENCY_COLLISION", "receipt family does not match action"
                    )
                return resolved
            key = state_key(tenant_org_id, request.scope_ref, request.object_id)
            current_doc = await self._objects.find_one(key, session=session)
            if current_doc is None:
                raise StateConflict("OBJECT_NOT_FOUND", "StateKey does not exist")
            lifecycle = str(current_doc.get("lifecycle"))
            if lifecycle == "erased":
                raise ContractRefused("OBJECT_ERASED", "erased state is terminal in v1")
            if lifecycle != "tombstoned":
                raise StateConflict(
                    "OBJECT_ALREADY_EXISTS", "restore requires current tombstoned state"
                )
            if int(current_doc["current_revision"]) != request.expected_revision:
                raise StateConflict(
                    "REVISION_CONFLICT", "expected revision does not match current revision"
                )
            if str(current_doc["state_digest"]) != request.expected_state_digest:
                raise StateConflict(
                    "STATE_DIGEST_CONFLICT", "expected state digest does not match current state"
                )
            if request.source_revision >= int(current_doc["current_revision"]):
                raise ContractRefused(
                    "RESTORE_SOURCE_UNAVAILABLE", "restore source must be a retained prior revision"
                )
            current_revision_doc = await self._revisions.find_one(
                {**key, "revision": int(current_doc["current_revision"])}, session=session
            )
            source_doc = await self._revisions.find_one(
                {**key, "revision": request.source_revision}, session=session
            )
            if current_revision_doc is None:
                raise StateInfrastructureFailure(
                    code="STORAGE_UNAVAILABLE",
                    message="current immutable revision is missing",
                    operation_id=request.operation_id,
                    scope_ref=request.scope_ref,
                )
            if source_doc is None or source_doc.get("payload") is None:
                raise ContractRefused(
                    "RESTORE_SOURCE_UNAVAILABLE", "retained restore source payload is unavailable"
                )
            current = self._revision_from_document(current_revision_doc)
            source = self._revision_from_document(source_doc)
            if (
                source.state_ref.schema_ref != current.state_ref.schema_ref
                or source.state_ref.schema_digest != current.state_ref.schema_digest
            ):
                raise ContractRefused(
                    "SCHEMA_BINDING_IMMUTABLE",
                    "restore source schema does not match object binding",
                )
            await self._guard_transition_claim(
                tenant_org_id=tenant_org_id,
                scope_ref=request.scope_ref,
                object_id=request.object_id,
                claim_id=request.claim_id,
                fencing_token=request.fencing_token,
                holder_ref=principal_ref,
                session=session,
            )
            pending, receipt, event = build_restore_artifacts(
                current=current,
                source=source,
                request=request,
                request_digest=request_digest,
                receipt_id=receipt_id,
                event_id=event_id,
                principal_ref=principal_ref,
                authorization_ref=authorization_ref,
                committed_at=logical_issued_at,
                causation_ref=causation_ref,
            )
            final_revision = StoredRevision(
                tenant_org_id=tenant_org_id,
                state_ref=pending.state_ref,
                payload=pending.payload,
                recorded_at=pending.recorded_at,
                retention_policy_ref=current.retention_policy_ref,
                retention_policy_digest=current.retention_policy_digest,
            )
            updated = await self._objects.update_one(
                {
                    **key,
                    "current_revision": request.expected_revision,
                    "state_digest": request.expected_state_digest,
                    "lifecycle": "tombstoned",
                },
                {"$set": self._current_document(final_revision)},
                session=session,
            )
            if updated.matched_count != 1:
                raise StateConflict("REVISION_CONFLICT", "state changed during restore")
            seq = await self._next_journal_seq(tenant_org_id, request.scope_ref, session)
            committed_event = event.model_copy(update={"journal_seq": seq})
            await self._revisions.insert_one(
                self._revision_document(final_revision), session=session
            )
            await self._events.insert_one(
                self._event_document(tenant_org_id, committed_event), session=session
            )
            operation = StoredOperation(action, request_digest, STATE_RECEIPT_SCHEMA_REF, receipt)
            await self._operations.insert_one(
                self._operation_document(tenant_org_id, request.scope_ref, operation),
                session=session,
            )
            return receipt

        return await self._run_mutation(
            tenant_org_id=tenant_org_id,
            scope_ref=request.scope_ref,
            operation_id=request.operation_id,
            action=action,
            request_digest=request_digest,
            receipt_schema_ref=STATE_RECEIPT_SCHEMA_REF,
            callback=callback,
            logical_receipt_factory=logical_receipt,
            budget=budget,
        )

    async def hard_erase(
        self,
        *,
        revision: PendingRevision,
        expected_revision: int,
        expected_state_digest: str,
        expected_retention_policy_ref: str,
        expected_retention_policy_digest: str,
        claim_id: str | None,
        fencing_token: int | None,
        operation: StoredOperation,
        event: StateTransitionEvent,
        budget: ExecutionBudget,
    ) -> HardEraseReceipt:
        tenant_org_id = revision.tenant_org_id
        scope_ref = revision.state_ref.scope_ref
        object_id = revision.state_ref.object_id
        operation_id = operation.receipt.operation_id
        if not isinstance(operation.receipt, HardEraseReceipt):
            raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")

        async def callback(session: Any) -> HardEraseReceipt:
            existing = await self._get_operation_raw(
                tenant_org_id, scope_ref, operation_id, session=session
            )
            resolved = self._resolve_existing(
                existing, action=operation.action, request_digest=operation.request_digest
            )
            if resolved is not None:
                if not isinstance(resolved, HardEraseReceipt):
                    raise StateConflict(
                        "IDEMPOTENCY_COLLISION", "receipt family does not match action"
                    )
                return resolved
            key = state_key(tenant_org_id, scope_ref, object_id)
            current_doc = await self._objects.find_one(key, session=session)
            if current_doc is None:
                raise StateConflict("OBJECT_NOT_FOUND", "StateKey does not exist")
            if str(current_doc.get("lifecycle")) == "erased":
                raise ContractRefused("OBJECT_ERASED", "object is already erased")
            if int(current_doc["current_revision"]) != expected_revision:
                raise StateConflict(
                    "REVISION_CONFLICT", "expected revision does not match current revision"
                )
            if str(current_doc["state_digest"]) != expected_state_digest:
                raise StateConflict(
                    "STATE_DIGEST_CONFLICT", "expected state digest does not match current state"
                )
            if (
                str(current_doc["retention_policy_ref"]) != expected_retention_policy_ref
                or str(current_doc["retention_policy_digest"]) != expected_retention_policy_digest
            ):
                raise ContractRefused(
                    "RETENTION_POLICY_NOT_ADMITTED",
                    "authorized retention decision does not match object policy binding",
                )
            if revision.state_ref.revision != expected_revision + 1:
                raise StateConflict(
                    "REVISION_CONFLICT", "erased revision must increment exactly once"
                )
            if revision.state_ref.lifecycle != "erased" or revision.payload is not None:
                raise ContractRefused(
                    "OBJECT_ERASED", "hard erase candidate must be erased with no payload"
                )
            if revision.state_ref.schema_ref != str(
                current_doc["schema_ref"]
            ) or revision.state_ref.schema_digest != str(current_doc["schema_digest"]):
                raise ContractRefused(
                    "SCHEMA_BINDING_IMMUTABLE", "hard erase cannot change object schema binding"
                )
            await self._guard_hard_erase_claim(
                tenant_org_id=tenant_org_id,
                scope_ref=scope_ref,
                object_id=object_id,
                claim_id=claim_id,
                fencing_token=fencing_token,
                session=session,
            )
            final_revision = StoredRevision(
                tenant_org_id=tenant_org_id,
                state_ref=revision.state_ref,
                payload=None,
                recorded_at=revision.recorded_at,
                retention_policy_ref=expected_retention_policy_ref,
                retention_policy_digest=expected_retention_policy_digest,
            )
            updated = await self._objects.update_one(
                {
                    **key,
                    "current_revision": expected_revision,
                    "state_digest": expected_state_digest,
                },
                {"$set": self._current_document(final_revision)},
                session=session,
            )
            if updated.matched_count != 1:
                raise StateConflict("REVISION_CONFLICT", "state changed during hard erase")
            await self._revisions.update_many(
                key,
                {"$set": {"payload": None}},
                session=session,
            )
            await self._revisions.insert_one(
                self._revision_document(final_revision), session=session
            )
            await self._claims.update_one(
                key,
                [{"$set": {"active": False, "released_at": "$$NOW"}}],
                session=session,
            )
            seq = await self._next_journal_seq(tenant_org_id, scope_ref, session)
            committed_event = event.model_copy(update={"journal_seq": seq})
            await self._events.insert_one(
                self._event_document(tenant_org_id, committed_event), session=session
            )
            await self._operations.insert_one(
                self._operation_document(tenant_org_id, scope_ref, operation), session=session
            )
            return cast(HardEraseReceipt, operation.receipt)

        return await self._run_mutation(
            tenant_org_id=tenant_org_id,
            scope_ref=scope_ref,
            operation_id=operation_id,
            action=operation.action,
            request_digest=operation.request_digest,
            receipt_schema_ref=operation.receipt_schema_ref,
            callback=callback,
            logical_receipt_factory=lambda exc: hard_erase_terminal_receipt(
                cast(HardEraseReceipt, operation.receipt), exc
            ),
            budget=budget,
        )

    async def claim(
        self,
        *,
        tenant_org_id: str,
        request: StateClaimRequest,
        request_digest: str,
        receipt_id: str,
        claim_id: str,
        principal_ref: str,
        authorization_ref: str,
        budget: ExecutionBudget,
    ) -> StateClaimReceipt:
        action: Literal["state.claim"] = "state.claim"
        logical_issued_at = datetime.now(UTC)

        async def callback(session: Any) -> StateClaimReceipt:
            existing = await self._get_operation_raw(
                tenant_org_id, request.scope_ref, request.operation_id, session=session
            )
            resolved = self._resolve_existing(
                existing, action=action, request_digest=request_digest
            )
            if resolved is not None:
                if not isinstance(resolved, StateClaimReceipt):
                    raise StateConflict(
                        "IDEMPOTENCY_COLLISION", "receipt family does not match action"
                    )
                return resolved
            key = state_key(tenant_org_id, request.scope_ref, request.object_id)
            current = await self._objects.find_one(
                key, projection={"current_revision": 1, "lifecycle": 1}, session=session
            )
            if current is None:
                raise StateConflict("OBJECT_NOT_FOUND", "StateKey does not exist")
            if str(current.get("lifecycle")) == "erased":
                raise ContractRefused("OBJECT_ERASED", "erased state is terminal in v1")
            if int(current["current_revision"]) != request.expected_revision:
                raise StateConflict("REVISION_CONFLICT", "claim expected revision is stale")
            claimed = await self._claims.find_one_and_update(
                acquire_claim_filter(tenant_org_id, request.scope_ref, request.object_id),
                acquire_claim_pipeline(
                    claim_id=claim_id,
                    holder_ref=principal_ref,
                    ttl_seconds=request.ttl_seconds,
                ),
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if claimed is None:
                active = await self._claims.find_one(
                    {
                        **key,
                        "$expr": {
                            "$and": [
                                {"$eq": ["$active", True]},
                                {"$gt": ["$expires_at", "$$NOW"]},
                            ]
                        },
                    },
                    session=session,
                )
                if active is not None:
                    raise StateConflict("CLAIM_ACTIVE", "StateKey already has an active claim")
                raise StateConflict("CLAIM_NOT_FOUND", "claim lineage missing or unavailable")
            issued_at = claimed["acquired_at"]
            receipt = StateClaimReceipt(
                contract_version="1.0",
                receipt_id=receipt_id,
                action=action,
                operation_id=request.operation_id,
                request_digest=request_digest,
                scope_ref=request.scope_ref,
                object_id=request.object_id,
                principal_ref=principal_ref,
                authorization_ref=authorization_ref,
                status="claimed",
                claim_id=claim_id,
                fencing_token=int(claimed["fencing_token"]),
                holder_ref=principal_ref,
                expires_at=claimed["expires_at"],
                issued_at=issued_at,
                reason_codes=(),
            )
            operation = StoredOperation(action, request_digest, CLAIM_RECEIPT_SCHEMA_REF, receipt)
            await self._operations.insert_one(
                self._operation_document(tenant_org_id, request.scope_ref, operation),
                session=session,
            )
            return receipt

        return await self._run_mutation(
            tenant_org_id=tenant_org_id,
            scope_ref=request.scope_ref,
            operation_id=request.operation_id,
            action=action,
            request_digest=request_digest,
            receipt_schema_ref=CLAIM_RECEIPT_SCHEMA_REF,
            callback=callback,
            logical_receipt_factory=lambda exc: claim_terminal_receipt(
                receipt_id=receipt_id,
                action=action,
                operation_id=request.operation_id,
                request_digest=request_digest,
                scope_ref=request.scope_ref,
                object_id=request.object_id,
                principal_ref=principal_ref,
                authorization_ref=authorization_ref,
                issued_at=logical_issued_at,
                exc=exc,
            ),
            budget=budget,
        )

    async def renew(
        self,
        *,
        tenant_org_id: str,
        request: StateRenewRequest,
        request_digest: str,
        receipt_id: str,
        principal_ref: str,
        authorization_ref: str,
        budget: ExecutionBudget,
    ) -> StateClaimReceipt:
        action: Literal["state.renew"] = "state.renew"
        logical_issued_at = datetime.now(UTC)

        async def callback(session: Any) -> StateClaimReceipt:
            existing = await self._get_operation_raw(
                tenant_org_id, request.scope_ref, request.operation_id, session=session
            )
            resolved = self._resolve_existing(
                existing, action=action, request_digest=request_digest
            )
            if resolved is not None:
                if not isinstance(resolved, StateClaimReceipt):
                    raise StateConflict(
                        "IDEMPOTENCY_COLLISION", "receipt family does not match action"
                    )
                return resolved
            renewed = await self._claims.find_one_and_update(
                exact_claim_filter(
                    tenant_org_id,
                    request.scope_ref,
                    request.object_id,
                    claim_id=request.claim_id,
                    fencing_token=request.fencing_token,
                    holder_ref=principal_ref,
                ),
                renew_claim_pipeline(request.ttl_seconds),
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if renewed is None:
                await self._classify_claim_failure(
                    tenant_org_id=tenant_org_id,
                    scope_ref=request.scope_ref,
                    object_id=request.object_id,
                    claim_id=request.claim_id,
                    fencing_token=request.fencing_token,
                    holder_ref=principal_ref,
                    session=session,
                )
                raise AssertionError("claim failure classifier must raise")
            receipt = StateClaimReceipt(
                contract_version="1.0",
                receipt_id=receipt_id,
                action=action,
                operation_id=request.operation_id,
                request_digest=request_digest,
                scope_ref=request.scope_ref,
                object_id=request.object_id,
                principal_ref=principal_ref,
                authorization_ref=authorization_ref,
                status="renewed",
                claim_id=request.claim_id,
                fencing_token=request.fencing_token,
                holder_ref=principal_ref,
                expires_at=renewed["expires_at"],
                issued_at=renewed["renewed_at"],
                reason_codes=(),
            )
            operation = StoredOperation(action, request_digest, CLAIM_RECEIPT_SCHEMA_REF, receipt)
            await self._operations.insert_one(
                self._operation_document(tenant_org_id, request.scope_ref, operation),
                session=session,
            )
            return receipt

        return await self._run_mutation(
            tenant_org_id=tenant_org_id,
            scope_ref=request.scope_ref,
            operation_id=request.operation_id,
            action=action,
            request_digest=request_digest,
            receipt_schema_ref=CLAIM_RECEIPT_SCHEMA_REF,
            callback=callback,
            logical_receipt_factory=lambda exc: claim_terminal_receipt(
                receipt_id=receipt_id,
                action=action,
                operation_id=request.operation_id,
                request_digest=request_digest,
                scope_ref=request.scope_ref,
                object_id=request.object_id,
                principal_ref=principal_ref,
                authorization_ref=authorization_ref,
                issued_at=logical_issued_at,
                exc=exc,
            ),
            budget=budget,
        )

    async def release(
        self,
        *,
        tenant_org_id: str,
        request: StateReleaseRequest,
        request_digest: str,
        receipt_id: str,
        principal_ref: str,
        authorization_ref: str,
        budget: ExecutionBudget,
    ) -> StateClaimReceipt:
        action: Literal["state.release"] = "state.release"
        logical_issued_at = datetime.now(UTC)

        async def callback(session: Any) -> StateClaimReceipt:
            existing = await self._get_operation_raw(
                tenant_org_id, request.scope_ref, request.operation_id, session=session
            )
            resolved = self._resolve_existing(
                existing, action=action, request_digest=request_digest
            )
            if resolved is not None:
                if not isinstance(resolved, StateClaimReceipt):
                    raise StateConflict(
                        "IDEMPOTENCY_COLLISION", "receipt family does not match action"
                    )
                return resolved
            released = await self._claims.find_one_and_update(
                exact_claim_filter(
                    tenant_org_id,
                    request.scope_ref,
                    request.object_id,
                    claim_id=request.claim_id,
                    fencing_token=request.fencing_token,
                    holder_ref=principal_ref,
                ),
                release_claim_pipeline(),
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if released is None:
                await self._classify_claim_failure(
                    tenant_org_id=tenant_org_id,
                    scope_ref=request.scope_ref,
                    object_id=request.object_id,
                    claim_id=request.claim_id,
                    fencing_token=request.fencing_token,
                    holder_ref=principal_ref,
                    session=session,
                )
                raise AssertionError("claim failure classifier must raise")
            receipt = StateClaimReceipt(
                contract_version="1.0",
                receipt_id=receipt_id,
                action=action,
                operation_id=request.operation_id,
                request_digest=request_digest,
                scope_ref=request.scope_ref,
                object_id=request.object_id,
                principal_ref=principal_ref,
                authorization_ref=authorization_ref,
                status="released",
                claim_id=request.claim_id,
                fencing_token=request.fencing_token,
                holder_ref=principal_ref,
                released_at=released["released_at"],
                issued_at=released["released_at"],
                reason_codes=(),
            )
            operation = StoredOperation(action, request_digest, CLAIM_RECEIPT_SCHEMA_REF, receipt)
            await self._operations.insert_one(
                self._operation_document(tenant_org_id, request.scope_ref, operation),
                session=session,
            )
            return receipt

        return await self._run_mutation(
            tenant_org_id=tenant_org_id,
            scope_ref=request.scope_ref,
            operation_id=request.operation_id,
            action=action,
            request_digest=request_digest,
            receipt_schema_ref=CLAIM_RECEIPT_SCHEMA_REF,
            callback=callback,
            logical_receipt_factory=lambda exc: claim_terminal_receipt(
                receipt_id=receipt_id,
                action=action,
                operation_id=request.operation_id,
                request_digest=request_digest,
                scope_ref=request.scope_ref,
                object_id=request.object_id,
                principal_ref=principal_ref,
                authorization_ref=authorization_ref,
                issued_at=logical_issued_at,
                exc=exc,
            ),
            budget=budget,
        )

    async def _current_journal_seq_unbounded(self, tenant_org_id: str, scope_ref: str) -> int:
        try:
            doc = await self._majority_collection(self._scope_counters).find_one(
                {"tenant_org_id": tenant_org_id, "scope_ref": scope_ref},
                projection={"next_journal_seq": 1},
            )
            return 0 if doc is None else int(doc.get("next_journal_seq", 0))
        except PyMongoError as exc:
            raise StateInfrastructureFailure(
                code="STORAGE_UNAVAILABLE",
                message="journal high-water lookup unavailable",
                scope_ref=scope_ref,
            ) from exc

    async def _list_state_refs_unbounded(
        self,
        tenant_org_id: str,
        scope_ref: str,
        *,
        schema_refs: tuple[str, ...],
        lifecycle: str,
        after_object_id: str | None,
        limit: int,
    ) -> tuple[tuple[StateRef, ...], bool]:
        query: dict[str, Any] = {
            "tenant_org_id": tenant_org_id,
            "scope_ref": scope_ref,
            "schema_ref": {"$in": list(schema_refs)},
            "lifecycle": lifecycle,
        }
        if after_object_id is not None:
            query["object_id"] = {"$gt": after_object_id}
        try:
            cursor = (
                self._majority_collection(self._objects)
                .find(query, projection={"state_ref": 1, "object_id": 1})
                .sort("object_id", 1)
                .limit(limit + 1)
            )
            docs: list[dict[str, Any]] = []
            async for doc in cursor:
                docs.append(doc)
            has_more = len(docs) > limit
            refs = tuple(StateRef.model_validate(doc["state_ref"]) for doc in docs[:limit])
            return refs, has_more
        except PyMongoError as exc:
            raise StateInfrastructureFailure(
                code="STORAGE_UNAVAILABLE",
                message="state list unavailable",
                scope_ref=scope_ref,
            ) from exc

    @staticmethod
    def _event_from_document(doc: dict[str, Any]) -> StateTransitionEvent:
        payload = {
            key: value
            for key, value in doc.items()
            if key not in {"_id", "tenant_org_id", "scope_ref", "object_id"}
        }
        return StateTransitionEvent.model_validate(payload)

    async def _read_events_unbounded(
        self,
        tenant_org_id: str,
        scope_ref: str,
        *,
        after_seq: int,
        limit: int,
    ) -> EventSlice:
        query = {"tenant_org_id": tenant_org_id, "scope_ref": scope_ref}
        try:
            high_water = await self._current_journal_seq_unbounded(tenant_org_id, scope_ref)
            oldest = await self._majority_collection(self._events).find_one(
                query, projection={"journal_seq": 1}, sort=[("journal_seq", 1)]
            )
            retained_from = high_water + 1 if oldest is None else int(oldest["journal_seq"])
            cursor = (
                self._majority_collection(self._events)
                .find({**query, "journal_seq": {"$gt": after_seq, "$lte": high_water}})
                .sort("journal_seq", 1)
                .limit(limit)
            )
            events: list[StateTransitionEvent] = []
            async for doc in cursor:
                events.append(self._event_from_document(doc))
            return EventSlice(tuple(events), high_water, retained_from)
        except StateInfrastructureFailure:
            raise
        except PyMongoError as exc:
            raise StateInfrastructureFailure(
                code="STORAGE_UNAVAILABLE",
                message="scope event journal unavailable",
                scope_ref=scope_ref,
            ) from exc

    async def _get_consumer_cursor_unbounded(
        self, tenant_org_id: str, scope_ref: str, consumer_ref: str
    ) -> int | None:
        try:
            doc = await self._majority_collection(self._consumer_cursors).find_one(
                {
                    "tenant_org_id": tenant_org_id,
                    "scope_ref": scope_ref,
                    "consumer_ref": consumer_ref,
                },
                projection={"journal_seq": 1},
            )
            return None if doc is None else int(doc["journal_seq"])
        except PyMongoError as exc:
            raise StateInfrastructureFailure(
                code="STORAGE_UNAVAILABLE",
                message="consumer checkpoint unavailable",
                scope_ref=scope_ref,
            ) from exc

    async def acknowledge(
        self,
        *,
        tenant_org_id: str,
        scope_ref: str,
        consumer_ref: str,
        cursor_seq: int,
        committed_cursor: str,
        operation: StoredOperation,
        budget: ExecutionBudget,
    ) -> StateAckReceipt:
        operation_id = operation.receipt.operation_id
        if not isinstance(operation.receipt, StateAckReceipt):
            raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")
        ack_receipt = operation.receipt

        async def callback(session: Any) -> StateAckReceipt:
            existing = await self._get_operation_raw(
                tenant_org_id, scope_ref, operation_id, session=session
            )
            resolved = self._resolve_existing(
                existing, action=operation.action, request_digest=operation.request_digest
            )
            if resolved is not None:
                if not isinstance(resolved, StateAckReceipt):
                    raise StateConflict(
                        "IDEMPOTENCY_COLLISION", "receipt family does not match action"
                    )
                return resolved
            key = {
                "tenant_org_id": tenant_org_id,
                "scope_ref": scope_ref,
                "consumer_ref": consumer_ref,
            }
            current = await self._consumer_cursors.find_one(
                key, projection={"journal_seq": 1}, session=session
            )
            if current is not None and int(current["journal_seq"]) > cursor_seq:
                raise StateConflict("ACK_REGRESSION", "consumer cursor cannot move backward")
            receipt = ack_receipt.model_copy(update={"committed_cursor": committed_cursor})
            await self._consumer_cursors.update_one(
                key,
                {
                    "$set": {
                        "journal_seq": cursor_seq,
                        "cursor": committed_cursor,
                        "updated_at": receipt.issued_at,
                    }
                },
                upsert=True,
                session=session,
            )
            terminal = StoredOperation(
                operation.action, operation.request_digest, ACK_RECEIPT_SCHEMA_REF, receipt
            )
            await self._operations.insert_one(
                self._operation_document(tenant_org_id, scope_ref, terminal), session=session
            )
            return receipt

        return await self._run_mutation(
            tenant_org_id=tenant_org_id,
            scope_ref=scope_ref,
            operation_id=operation_id,
            action=operation.action,
            request_digest=operation.request_digest,
            receipt_schema_ref=ACK_RECEIPT_SCHEMA_REF,
            callback=callback,
            logical_receipt_factory=None,
            budget=budget,
        )

    async def _read_history_unbounded(
        self,
        tenant_org_id: str,
        scope_ref: str,
        object_id: str,
        *,
        after_revision: int,
        limit: int,
    ) -> HistorySlice | None:
        key = state_key(tenant_org_id, scope_ref, object_id)
        try:
            current = await self._majority_collection(self._objects).find_one(
                key, projection={"current_revision": 1}
            )
            if current is None:
                return None
            latest = int(current["current_revision"])
            base_query = {
                "tenant_org_id": tenant_org_id,
                "scope_ref": scope_ref,
                "object_id": object_id,
            }
            oldest = await self._majority_collection(self._events).find_one(
                base_query, projection={"state_ref.revision": 1}, sort=[("state_ref.revision", 1)]
            )
            oldest_available = latest if oldest is None else int(oldest["state_ref"]["revision"])
            cursor = (
                self._majority_collection(self._events)
                .find({**base_query, "state_ref.revision": {"$gt": after_revision, "$lte": latest}})
                .sort("state_ref.revision", 1)
                .limit(limit)
            )
            events: list[StateTransitionEvent] = []
            async for doc in cursor:
                events.append(self._event_from_document(doc))
            return HistorySlice(tuple(events), oldest_available, latest)
        except PyMongoError as exc:
            raise StateInfrastructureFailure(
                code="STORAGE_UNAVAILABLE",
                message="state history unavailable",
                scope_ref=scope_ref,
            ) from exc

    # ------------------------------------------------------------------ bounded reads (F-03)
    async def get_revision(
        self,
        tenant_org_id: str,
        scope_ref: str,
        object_id: str,
        revision: int | None = None,
        *,
        budget: ExecutionBudget | None = None,
    ) -> StoredRevision | None:
        return await self._bounded_read(
            budget,
            self._get_revision_unbounded(tenant_org_id, scope_ref, object_id, revision),
            scope_ref=scope_ref,
        )

    async def current_journal_seq(
        self, tenant_org_id: str, scope_ref: str, *, budget: ExecutionBudget | None = None
    ) -> int:
        return await self._bounded_read(
            budget,
            self._current_journal_seq_unbounded(tenant_org_id, scope_ref),
            scope_ref=scope_ref,
        )

    async def list_state_refs(
        self,
        tenant_org_id: str,
        scope_ref: str,
        *,
        schema_refs: tuple[str, ...],
        lifecycle: str,
        after_object_id: str | None,
        limit: int,
        budget: ExecutionBudget | None = None,
    ) -> tuple[tuple[StateRef, ...], bool]:
        return await self._bounded_read(
            budget,
            self._list_state_refs_unbounded(
                tenant_org_id,
                scope_ref,
                schema_refs=schema_refs,
                lifecycle=lifecycle,
                after_object_id=after_object_id,
                limit=limit,
            ),
            scope_ref=scope_ref,
        )

    async def read_events(
        self,
        tenant_org_id: str,
        scope_ref: str,
        *,
        after_seq: int,
        limit: int,
        budget: ExecutionBudget | None = None,
    ) -> EventSlice:
        return await self._bounded_read(
            budget,
            self._read_events_unbounded(tenant_org_id, scope_ref, after_seq=after_seq, limit=limit),
            scope_ref=scope_ref,
        )

    async def get_consumer_cursor(
        self,
        tenant_org_id: str,
        scope_ref: str,
        consumer_ref: str,
        *,
        budget: ExecutionBudget | None = None,
    ) -> int | None:
        return await self._bounded_read(
            budget,
            self._get_consumer_cursor_unbounded(tenant_org_id, scope_ref, consumer_ref),
            scope_ref=scope_ref,
        )

    async def read_history(
        self,
        tenant_org_id: str,
        scope_ref: str,
        object_id: str,
        *,
        after_revision: int,
        limit: int,
        budget: ExecutionBudget | None = None,
    ) -> HistorySlice | None:
        return await self._bounded_read(
            budget,
            self._read_history_unbounded(
                tenant_org_id, scope_ref, object_id, after_revision=after_revision, limit=limit
            ),
            scope_ref=scope_ref,
        )
