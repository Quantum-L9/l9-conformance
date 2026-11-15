from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Literal

from ..errors import ContractRefused, StateConflict, StateError
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
    StoredClaim,
    StoredOperation,
    StoredRevision,
)


class InProcessStateStore:
    """Atomic single-process conformance store. Tests only; not a runtime provider."""

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._lock = asyncio.Lock()
        self._now = now or (lambda: datetime.now(UTC))
        self._revisions: dict[tuple[str, str, str, int], StoredRevision] = {}
        self._current: dict[tuple[str, str, str], int] = {}
        self._operations: dict[tuple[str, str, str], StoredOperation] = {}
        self._events: dict[tuple[str, str], list[StateTransitionEvent]] = defaultdict(list)
        self._claims: dict[tuple[str, str, str], StoredClaim] = {}
        self._consumer_cursors: dict[tuple[str, str, str], int] = {}
        self._scope_seq: dict[tuple[str, str], int] = defaultdict(int)

    @staticmethod
    def _object_key(tenant_org_id: str, scope_ref: str, object_id: str) -> tuple[str, str, str]:
        return tenant_org_id, scope_ref, object_id

    @staticmethod
    def _operation_key(
        tenant_org_id: str, scope_ref: str, operation_id: str
    ) -> tuple[str, str, str]:
        return tenant_org_id, scope_ref, operation_id

    def _next_seq(self, tenant_org_id: str, scope_ref: str) -> int:
        key = (tenant_org_id, scope_ref)
        self._scope_seq[key] += 1
        return self._scope_seq[key]

    def _resolve_existing_operation(
        self, *, key: tuple[str, str, str], action: str, request_digest: str
    ) -> StoredOperation | None:
        existing = self._operations.get(key)
        if existing is None:
            return None
        if existing.action == action and existing.request_digest == request_digest:
            return existing
        raise StateConflict(
            "IDEMPOTENCY_COLLISION", "operation_id already binds different semantics"
        )

    @staticmethod
    def _claim_is_active(claim: StoredClaim, now: datetime) -> bool:
        return bool(claim.active and claim.expires_at is not None and claim.expires_at > now)

    def _assert_supplied_claim(
        self,
        claim: StoredClaim,
        *,
        claim_id: str,
        fencing_token: int,
        holder_ref: str,
        now: datetime,
    ) -> None:
        if claim.claim_id is None:
            raise StateConflict("CLAIM_NOT_FOUND", "no claim exists for StateKey")
        if claim.claim_id != claim_id or claim.fencing_token != fencing_token:
            raise StateConflict("CLAIM_STALE_FENCE", "claim identity or fence is stale")
        if claim.holder_ref != holder_ref:
            raise ContractRefused("CLAIM_HOLDER_MISMATCH", "claim belongs to another holder")
        if not claim.active:
            raise StateConflict("CLAIM_NOT_FOUND", "claim has been released")
        if claim.expires_at is None or claim.expires_at <= now:
            raise StateConflict("CLAIM_EXPIRED", "claim has expired")

    def _guard_transition(
        self,
        claim: StoredClaim,
        *,
        claim_id: str | None,
        fencing_token: int | None,
        holder_ref: str,
        now: datetime,
    ) -> StoredClaim:
        active = self._claim_is_active(claim, now)
        if claim_id is None:
            if active:
                raise StateConflict(
                    "CLAIM_REQUIRED", "an active claim requires claim identity and fence"
                )
        else:
            assert fencing_token is not None
            self._assert_supplied_claim(
                claim,
                claim_id=claim_id,
                fencing_token=fencing_token,
                holder_ref=holder_ref,
                now=now,
            )
        return StoredClaim(
            tenant_org_id=claim.tenant_org_id,
            scope_ref=claim.scope_ref,
            object_id=claim.object_id,
            highest_fence=claim.highest_fence,
            active=claim.active,
            claim_id=claim.claim_id,
            fencing_token=claim.fencing_token,
            holder_ref=claim.holder_ref,
            acquired_at=claim.acquired_at,
            expires_at=claim.expires_at,
            released_at=claim.released_at,
            guard_version=claim.guard_version + 1,
        )

    def _guard_hard_erase(
        self,
        claim: StoredClaim,
        *,
        claim_id: str | None,
        fencing_token: int | None,
        now: datetime,
    ) -> StoredClaim:
        """Fence retention-authorized erase without requiring claim-holder impersonation."""
        active = self._claim_is_active(claim, now)
        if claim_id is None:
            if active:
                raise StateConflict(
                    "CLAIM_REQUIRED", "an active claim requires exact claim and fence"
                )
        else:
            assert fencing_token is not None
            if claim.claim_id is None:
                raise StateConflict("CLAIM_NOT_FOUND", "no claim exists for StateKey")
            if claim.claim_id != claim_id or claim.fencing_token != fencing_token:
                raise StateConflict("CLAIM_STALE_FENCE", "claim identity or fence is stale")
            if not claim.active:
                raise StateConflict("CLAIM_NOT_FOUND", "claim has been released")
            if claim.expires_at is None or claim.expires_at <= now:
                raise StateConflict("CLAIM_EXPIRED", "claim has expired")
        return StoredClaim(
            tenant_org_id=claim.tenant_org_id,
            scope_ref=claim.scope_ref,
            object_id=claim.object_id,
            highest_fence=claim.highest_fence,
            active=claim.active,
            claim_id=claim.claim_id,
            fencing_token=claim.fencing_token,
            holder_ref=claim.holder_ref,
            acquired_at=claim.acquired_at,
            expires_at=claim.expires_at,
            released_at=claim.released_at,
            guard_version=claim.guard_version + 1,
        )

    def _persist_state_terminal(
        self,
        *,
        op_key: tuple[str, str, str],
        operation: StoredOperation,
        exc: StateError,
    ) -> StateReceipt:
        if exc.code == "IDEMPOTENCY_COLLISION":
            raise exc
        if not isinstance(operation.receipt, StateReceipt):
            raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")
        receipt = state_terminal_receipt(operation.receipt, exc)
        self._operations[op_key] = StoredOperation(
            operation.action, operation.request_digest, operation.receipt_schema_ref, receipt
        )
        return deepcopy(receipt)

    def _persist_state_terminal_fields(
        self,
        *,
        op_key: tuple[str, str, str],
        action: Literal["state.tombstone", "state.restore"],
        operation_id: str,
        request_digest: str,
        scope_ref: str,
        receipt_id: str,
        principal_ref: str,
        authorization_ref: str,
        issued_at: datetime,
        exc: StateError,
    ) -> StateReceipt:
        if exc.code == "IDEMPOTENCY_COLLISION":
            raise exc
        receipt = StateReceipt(
            contract_version="1.0",
            receipt_id=receipt_id,
            action=action,
            operation_id=operation_id,
            request_digest=request_digest,
            scope_ref=scope_ref,
            principal_ref=principal_ref,
            authorization_ref=authorization_ref,
            status=terminal_status(exc),
            issued_at=issued_at,
            reason_codes=(exc.code,),
        )
        self._operations[op_key] = StoredOperation(
            action, request_digest, STATE_RECEIPT_SCHEMA_REF, receipt
        )
        return deepcopy(receipt)

    def _persist_hard_erase_terminal(
        self,
        *,
        op_key: tuple[str, str, str],
        operation: StoredOperation,
        exc: StateError,
    ) -> HardEraseReceipt:
        if exc.code == "IDEMPOTENCY_COLLISION":
            raise exc
        if not isinstance(operation.receipt, HardEraseReceipt):
            raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")
        receipt = hard_erase_terminal_receipt(operation.receipt, exc)
        self._operations[op_key] = StoredOperation(
            operation.action, operation.request_digest, operation.receipt_schema_ref, receipt
        )
        return deepcopy(receipt)

    def _persist_claim_terminal(
        self,
        *,
        op_key: tuple[str, str, str],
        action: Literal["state.claim", "state.renew", "state.release"],
        request: StateClaimRequest | StateRenewRequest | StateReleaseRequest,
        request_digest: str,
        receipt_id: str,
        principal_ref: str,
        authorization_ref: str,
        issued_at: datetime,
        exc: StateError,
    ) -> StateClaimReceipt:
        if exc.code == "IDEMPOTENCY_COLLISION":
            raise exc
        receipt = claim_terminal_receipt(
            receipt_id=receipt_id,
            action=action,
            operation_id=request.operation_id,
            request_digest=request_digest,
            scope_ref=request.scope_ref,
            object_id=request.object_id,
            principal_ref=principal_ref,
            authorization_ref=authorization_ref,
            issued_at=issued_at,
            exc=exc,
        )
        self._operations[op_key] = StoredOperation(
            action, request_digest, CLAIM_RECEIPT_SCHEMA_REF, receipt
        )
        return deepcopy(receipt)

    async def get_revision(
        self,
        tenant_org_id: str,
        scope_ref: str,
        object_id: str,
        revision: int | None = None,
        *,
        budget: ExecutionBudget | None = None,
    ) -> StoredRevision | None:
        key = self._object_key(tenant_org_id, scope_ref, object_id)
        resolved = self._current.get(key) if revision is None else revision
        if resolved is None:
            return None
        return deepcopy(self._revisions.get((*key, resolved)))

    async def get_operation(
        self,
        tenant_org_id: str,
        scope_ref: str,
        operation_id: str,
        *,
        budget: ExecutionBudget | None = None,
    ) -> StoredOperation | None:
        # The in-process adapter ignores `budget`. This lookup is synchronous, but its other
        # budgeted reads await the adapter lock with no deadline, so a read can wait as long as a
        # concurrent mutation holds the lock. The F-03 budget bound is enforced by the provider
        # adapter (MongoStateStore._bounded_read), not here.
        return deepcopy(
            self._operations.get(self._operation_key(tenant_org_id, scope_ref, operation_id))
        )

    async def get_claim(
        self, tenant_org_id: str, scope_ref: str, object_id: str
    ) -> StoredClaim | None:
        return deepcopy(self._claims.get(self._object_key(tenant_org_id, scope_ref, object_id)))

    async def record_operation(
        self,
        *,
        tenant_org_id: str,
        scope_ref: str,
        operation: StoredOperation,
        budget: ExecutionBudget,
    ) -> MutationReceipt:
        async with self._lock:
            op_key = self._operation_key(tenant_org_id, scope_ref, operation.receipt.operation_id)
            existing = self._resolve_existing_operation(
                key=op_key, action=operation.action, request_digest=operation.request_digest
            )
            if existing is not None:
                return deepcopy(existing.receipt)
            budget.require_mutation_start(
                operation_id=operation.receipt.operation_id, scope_ref=scope_ref
            )
            self._operations[op_key] = deepcopy(operation)
            return deepcopy(operation.receipt)

    async def create(
        self,
        *,
        revision: StoredRevision,
        operation: StoredOperation,
        event: StateTransitionEvent,
        budget: ExecutionBudget,
    ) -> StateReceipt:
        async with self._lock:
            key = self._object_key(
                revision.tenant_org_id, revision.state_ref.scope_ref, revision.state_ref.object_id
            )
            op_key = self._operation_key(
                revision.tenant_org_id,
                revision.state_ref.scope_ref,
                operation.receipt.operation_id,
            )
            existing_op = self._resolve_existing_operation(
                key=op_key, action=operation.action, request_digest=operation.request_digest
            )
            if existing_op is not None:
                if not isinstance(existing_op.receipt, StateReceipt):
                    raise StateConflict(
                        "IDEMPOTENCY_COLLISION", "receipt family does not match action"
                    )
                return deepcopy(existing_op.receipt)
            budget.require_mutation_start(
                operation_id=operation.receipt.operation_id,
                scope_ref=revision.state_ref.scope_ref,
            )
            if key in self._current:
                existing_revision = self._revisions[(*key, self._current[key])]
                if existing_revision.state_ref.lifecycle in {"tombstoned", "erased"}:
                    exc: StateError = ContractRefused(
                        "OBJECT_ID_REUSE_FORBIDDEN",
                        "StateKey identity cannot be reused after lifecycle creation",
                    )
                else:
                    exc = StateConflict("OBJECT_ALREADY_EXISTS", "StateKey already exists")
                return self._persist_state_terminal(
                    op_key=op_key,
                    operation=operation,
                    exc=exc,
                )
            committed_event = event.model_copy(
                update={
                    "journal_seq": self._next_seq(
                        revision.tenant_org_id, revision.state_ref.scope_ref
                    )
                }
            )
            self._revisions[(*key, revision.state_ref.revision)] = deepcopy(revision)
            self._current[key] = revision.state_ref.revision
            self._events[(revision.tenant_org_id, revision.state_ref.scope_ref)].append(
                deepcopy(committed_event)
            )
            self._claims[key] = StoredClaim(
                tenant_org_id=revision.tenant_org_id,
                scope_ref=revision.state_ref.scope_ref,
                object_id=revision.state_ref.object_id,
                highest_fence=0,
                active=False,
            )
            self._operations[op_key] = deepcopy(operation)
            if not isinstance(operation.receipt, StateReceipt):
                raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")
            return deepcopy(operation.receipt)

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
        async with self._lock:
            key = self._object_key(
                revision.tenant_org_id, revision.state_ref.scope_ref, revision.state_ref.object_id
            )
            op_key = self._operation_key(
                revision.tenant_org_id,
                revision.state_ref.scope_ref,
                operation.receipt.operation_id,
            )
            existing_op = self._resolve_existing_operation(
                key=op_key, action=operation.action, request_digest=operation.request_digest
            )
            if existing_op is not None:
                if not isinstance(existing_op.receipt, StateReceipt):
                    raise StateConflict(
                        "IDEMPOTENCY_COLLISION", "receipt family does not match action"
                    )
                return deepcopy(existing_op.receipt)
            budget.require_mutation_start(
                operation_id=operation.receipt.operation_id,
                scope_ref=revision.state_ref.scope_ref,
            )
            current_revision = self._current.get(key)
            try:
                if current_revision is None:
                    raise StateConflict("OBJECT_NOT_FOUND", "StateKey does not exist")
                current = self._revisions[(*key, current_revision)]
                if current.state_ref.lifecycle == "tombstoned":
                    raise ContractRefused(
                        "OBJECT_TOMBSTONED", "ordinary transition requires active state"
                    )
                if current.state_ref.lifecycle == "erased":
                    raise ContractRefused("OBJECT_ERASED", "erased state is terminal in v1")
                if current.state_ref.revision != expected_revision:
                    raise StateConflict(
                        "REVISION_CONFLICT", "expected revision does not match current revision"
                    )
                if current.state_ref.state_digest != expected_state_digest:
                    raise StateConflict(
                        "STATE_DIGEST_CONFLICT",
                        "expected state digest does not match current state",
                    )
                if current.state_ref.schema_ref != expected_schema_ref:
                    raise ContractRefused(
                        "SCHEMA_BINDING_IMMUTABLE",
                        "expected schema ref does not match object binding",
                    )
                if current.state_ref.schema_digest != expected_schema_digest:
                    raise ContractRefused(
                        "SCHEMA_DIGEST_MISMATCH",
                        "expected schema digest does not match object binding",
                    )
                claim = self._claims.get(key)
                if claim is None:
                    raise StateConflict("CLAIM_NOT_FOUND", "claim lineage missing for StateKey")
                next_claim = self._guard_transition(
                    claim,
                    claim_id=claim_id,
                    fencing_token=fencing_token,
                    holder_ref=holder_ref,
                    now=self._now(),
                )
            except (StateConflict, ContractRefused) as exc:
                return self._persist_state_terminal(op_key=op_key, operation=operation, exc=exc)

            final_revision = StoredRevision(
                tenant_org_id=revision.tenant_org_id,
                state_ref=revision.state_ref,
                payload=deepcopy(revision.payload),
                recorded_at=revision.recorded_at,
                retention_policy_ref=current.retention_policy_ref,
                retention_policy_digest=current.retention_policy_digest,
            )
            self._claims[key] = next_claim
            committed_event = event.model_copy(
                update={
                    "journal_seq": self._next_seq(
                        revision.tenant_org_id, revision.state_ref.scope_ref
                    )
                }
            )
            self._revisions[(*key, revision.state_ref.revision)] = final_revision
            self._current[key] = revision.state_ref.revision
            self._events[(revision.tenant_org_id, revision.state_ref.scope_ref)].append(
                deepcopy(committed_event)
            )
            self._operations[op_key] = deepcopy(operation)
            if not isinstance(operation.receipt, StateReceipt):
                raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")
            return deepcopy(operation.receipt)

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
        async with self._lock:
            op_key = self._operation_key(tenant_org_id, request.scope_ref, request.operation_id)
            existing = self._resolve_existing_operation(
                key=op_key, action=action, request_digest=request_digest
            )
            if existing is not None:
                if not isinstance(existing.receipt, StateReceipt):
                    raise StateConflict(
                        "IDEMPOTENCY_COLLISION", "receipt family does not match action"
                    )
                return deepcopy(existing.receipt)
            budget.require_mutation_start(
                operation_id=request.operation_id, scope_ref=request.scope_ref
            )
            key = self._object_key(tenant_org_id, request.scope_ref, request.object_id)
            now = self._now()
            try:
                current_revision = self._current.get(key)
                if current_revision is None:
                    raise StateConflict("OBJECT_NOT_FOUND", "StateKey does not exist")
                current = self._revisions[(*key, current_revision)]
                if current.state_ref.lifecycle == "tombstoned":
                    raise ContractRefused("OBJECT_TOMBSTONED", "object is already tombstoned")
                if current.state_ref.lifecycle == "erased":
                    raise ContractRefused("OBJECT_ERASED", "erased state is terminal in v1")
                if current.state_ref.revision != request.expected_revision:
                    raise StateConflict(
                        "REVISION_CONFLICT", "expected revision does not match current revision"
                    )
                if current.state_ref.state_digest != request.expected_state_digest:
                    raise StateConflict(
                        "STATE_DIGEST_CONFLICT",
                        "expected state digest does not match current state",
                    )
                claim = self._claims.get(key)
                if claim is None:
                    raise StateConflict("CLAIM_NOT_FOUND", "claim lineage missing for StateKey")
                next_claim = self._guard_transition(
                    claim,
                    claim_id=request.claim_id,
                    fencing_token=request.fencing_token,
                    holder_ref=principal_ref,
                    now=now,
                )
            except (StateConflict, ContractRefused) as exc:
                return self._persist_state_terminal_fields(
                    op_key=op_key,
                    action=action,
                    operation_id=request.operation_id,
                    request_digest=request_digest,
                    scope_ref=request.scope_ref,
                    receipt_id=receipt_id,
                    principal_ref=principal_ref,
                    authorization_ref=authorization_ref,
                    issued_at=now,
                    exc=exc,
                )

            pending, receipt, event = build_tombstone_artifacts(
                current=current,
                request=request,
                request_digest=request_digest,
                receipt_id=receipt_id,
                event_id=event_id,
                principal_ref=principal_ref,
                authorization_ref=authorization_ref,
                committed_at=now,
                causation_ref=causation_ref,
            )
            final_revision = StoredRevision(
                tenant_org_id=tenant_org_id,
                state_ref=pending.state_ref,
                payload=deepcopy(pending.payload),
                recorded_at=now,
                retention_policy_ref=current.retention_policy_ref,
                retention_policy_digest=current.retention_policy_digest,
            )
            committed_event = event.model_copy(
                update={"journal_seq": self._next_seq(tenant_org_id, request.scope_ref)}
            )
            self._claims[key] = next_claim
            self._revisions[(*key, final_revision.state_ref.revision)] = final_revision
            self._current[key] = final_revision.state_ref.revision
            self._events[(tenant_org_id, request.scope_ref)].append(deepcopy(committed_event))
            self._operations[op_key] = StoredOperation(
                action, request_digest, STATE_RECEIPT_SCHEMA_REF, receipt
            )
            return deepcopy(receipt)

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
        async with self._lock:
            op_key = self._operation_key(tenant_org_id, request.scope_ref, request.operation_id)
            existing = self._resolve_existing_operation(
                key=op_key, action=action, request_digest=request_digest
            )
            if existing is not None:
                if not isinstance(existing.receipt, StateReceipt):
                    raise StateConflict(
                        "IDEMPOTENCY_COLLISION", "receipt family does not match action"
                    )
                return deepcopy(existing.receipt)
            budget.require_mutation_start(
                operation_id=request.operation_id, scope_ref=request.scope_ref
            )
            key = self._object_key(tenant_org_id, request.scope_ref, request.object_id)
            now = self._now()
            try:
                current_revision = self._current.get(key)
                if current_revision is None:
                    raise StateConflict("OBJECT_NOT_FOUND", "StateKey does not exist")
                current = self._revisions[(*key, current_revision)]
                if current.state_ref.lifecycle == "erased":
                    raise ContractRefused("OBJECT_ERASED", "erased state is terminal in v1")
                if current.state_ref.lifecycle != "tombstoned":
                    raise StateConflict(
                        "OBJECT_ALREADY_EXISTS", "restore requires current tombstoned state"
                    )
                if current.state_ref.revision != request.expected_revision:
                    raise StateConflict(
                        "REVISION_CONFLICT", "expected revision does not match current revision"
                    )
                if current.state_ref.state_digest != request.expected_state_digest:
                    raise StateConflict(
                        "STATE_DIGEST_CONFLICT",
                        "expected state digest does not match current state",
                    )
                if request.source_revision >= current.state_ref.revision:
                    raise ContractRefused(
                        "RESTORE_SOURCE_UNAVAILABLE",
                        "restore source must be a retained prior revision",
                    )
                source = self._revisions.get((*key, request.source_revision))
                if source is None or source.payload is None:
                    raise ContractRefused(
                        "RESTORE_SOURCE_UNAVAILABLE",
                        "retained restore source payload is unavailable",
                    )
                if (
                    source.state_ref.schema_ref != current.state_ref.schema_ref
                    or source.state_ref.schema_digest != current.state_ref.schema_digest
                ):
                    raise ContractRefused(
                        "SCHEMA_BINDING_IMMUTABLE",
                        "restore source schema does not match object binding",
                    )
                claim = self._claims.get(key)
                if claim is None:
                    raise StateConflict("CLAIM_NOT_FOUND", "claim lineage missing for StateKey")
                next_claim = self._guard_transition(
                    claim,
                    claim_id=request.claim_id,
                    fencing_token=request.fencing_token,
                    holder_ref=principal_ref,
                    now=now,
                )
            except (StateConflict, ContractRefused) as exc:
                return self._persist_state_terminal_fields(
                    op_key=op_key,
                    action=action,
                    operation_id=request.operation_id,
                    request_digest=request_digest,
                    scope_ref=request.scope_ref,
                    receipt_id=receipt_id,
                    principal_ref=principal_ref,
                    authorization_ref=authorization_ref,
                    issued_at=now,
                    exc=exc,
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
                committed_at=now,
                causation_ref=causation_ref,
            )
            final_revision = StoredRevision(
                tenant_org_id=tenant_org_id,
                state_ref=pending.state_ref,
                payload=deepcopy(pending.payload),
                recorded_at=now,
                retention_policy_ref=current.retention_policy_ref,
                retention_policy_digest=current.retention_policy_digest,
            )
            committed_event = event.model_copy(
                update={"journal_seq": self._next_seq(tenant_org_id, request.scope_ref)}
            )
            self._claims[key] = next_claim
            self._revisions[(*key, final_revision.state_ref.revision)] = final_revision
            self._current[key] = final_revision.state_ref.revision
            self._events[(tenant_org_id, request.scope_ref)].append(deepcopy(committed_event))
            self._operations[op_key] = StoredOperation(
                action, request_digest, STATE_RECEIPT_SCHEMA_REF, receipt
            )
            return deepcopy(receipt)

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
        async with self._lock:
            key = self._object_key(
                revision.tenant_org_id, revision.state_ref.scope_ref, revision.state_ref.object_id
            )
            op_key = self._operation_key(
                revision.tenant_org_id,
                revision.state_ref.scope_ref,
                operation.receipt.operation_id,
            )
            existing = self._resolve_existing_operation(
                key=op_key, action=operation.action, request_digest=operation.request_digest
            )
            if existing is not None:
                if not isinstance(existing.receipt, HardEraseReceipt):
                    raise StateConflict(
                        "IDEMPOTENCY_COLLISION", "receipt family does not match action"
                    )
                return deepcopy(existing.receipt)
            budget.require_mutation_start(
                operation_id=operation.receipt.operation_id,
                scope_ref=revision.state_ref.scope_ref,
            )
            now = self._now()
            try:
                current_revision = self._current.get(key)
                if current_revision is None:
                    raise StateConflict("OBJECT_NOT_FOUND", "StateKey does not exist")
                current = self._revisions[(*key, current_revision)]
                if current.state_ref.lifecycle == "erased":
                    raise ContractRefused("OBJECT_ERASED", "object is already erased")
                if current.state_ref.revision != expected_revision:
                    raise StateConflict(
                        "REVISION_CONFLICT", "expected revision does not match current revision"
                    )
                if current.state_ref.state_digest != expected_state_digest:
                    raise StateConflict(
                        "STATE_DIGEST_CONFLICT",
                        "expected state digest does not match current state",
                    )
                if (
                    current.retention_policy_ref != expected_retention_policy_ref
                    or current.retention_policy_digest != expected_retention_policy_digest
                ):
                    raise ContractRefused(
                        "RETENTION_POLICY_NOT_ADMITTED",
                        "authorized retention decision does not match object policy binding",
                    )
                if revision.state_ref.revision != current.state_ref.revision + 1:
                    raise StateConflict(
                        "REVISION_CONFLICT", "erased revision must increment exactly once"
                    )
                if revision.state_ref.lifecycle != "erased" or revision.payload is not None:
                    raise ContractRefused(
                        "OBJECT_ERASED", "hard erase candidate must be erased with no payload"
                    )
                if (
                    revision.state_ref.schema_ref != current.state_ref.schema_ref
                    or revision.state_ref.schema_digest != current.state_ref.schema_digest
                ):
                    raise ContractRefused(
                        "SCHEMA_BINDING_IMMUTABLE", "hard erase cannot change object schema binding"
                    )
                claim = self._claims.get(key)
                if claim is None:
                    raise StateConflict("CLAIM_NOT_FOUND", "claim lineage missing for StateKey")
                guarded_claim = self._guard_hard_erase(
                    claim,
                    claim_id=claim_id,
                    fencing_token=fencing_token,
                    now=now,
                )
            except (StateConflict, ContractRefused) as exc:
                return self._persist_hard_erase_terminal(
                    op_key=op_key, operation=operation, exc=exc
                )

            if not isinstance(operation.receipt, HardEraseReceipt):
                raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")
            final_revision = StoredRevision(
                tenant_org_id=revision.tenant_org_id,
                state_ref=revision.state_ref,
                payload=None,
                recorded_at=revision.recorded_at,
                retention_policy_ref=current.retention_policy_ref,
                retention_policy_digest=current.retention_policy_digest,
            )
            # Scrub retained payload bytes while preserving immutable revision identity/metadata.
            for revision_key, stored in list(self._revisions.items()):
                if revision_key[:3] != key:
                    continue
                self._revisions[revision_key] = StoredRevision(
                    tenant_org_id=stored.tenant_org_id,
                    state_ref=stored.state_ref,
                    payload=None,
                    recorded_at=stored.recorded_at,
                    retention_policy_ref=stored.retention_policy_ref,
                    retention_policy_digest=stored.retention_policy_digest,
                )
            self._revisions[(*key, final_revision.state_ref.revision)] = final_revision
            self._current[key] = final_revision.state_ref.revision
            self._claims[key] = StoredClaim(
                tenant_org_id=guarded_claim.tenant_org_id,
                scope_ref=guarded_claim.scope_ref,
                object_id=guarded_claim.object_id,
                highest_fence=guarded_claim.highest_fence,
                active=False,
                claim_id=guarded_claim.claim_id,
                fencing_token=guarded_claim.fencing_token,
                holder_ref=guarded_claim.holder_ref,
                acquired_at=guarded_claim.acquired_at,
                expires_at=guarded_claim.expires_at,
                released_at=now,
                guard_version=guarded_claim.guard_version,
            )
            committed_event = event.model_copy(
                update={
                    "journal_seq": self._next_seq(
                        revision.tenant_org_id, revision.state_ref.scope_ref
                    )
                }
            )
            self._events[(revision.tenant_org_id, revision.state_ref.scope_ref)].append(
                deepcopy(committed_event)
            )
            self._operations[op_key] = deepcopy(operation)
            return deepcopy(operation.receipt)

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
        async with self._lock:
            op_key = self._operation_key(tenant_org_id, request.scope_ref, request.operation_id)
            existing = self._resolve_existing_operation(
                key=op_key, action=action, request_digest=request_digest
            )
            if existing is not None:
                if not isinstance(existing.receipt, StateClaimReceipt):
                    raise StateConflict(
                        "IDEMPOTENCY_COLLISION", "receipt family does not match action"
                    )
                return deepcopy(existing.receipt)
            budget.require_mutation_start(
                operation_id=request.operation_id, scope_ref=request.scope_ref
            )
            key = self._object_key(tenant_org_id, request.scope_ref, request.object_id)
            now = self._now()
            try:
                current_revision = self._current.get(key)
                if current_revision is None:
                    raise StateConflict("OBJECT_NOT_FOUND", "StateKey does not exist")
                current = self._revisions[(*key, current_revision)]
                if current.state_ref.lifecycle == "erased":
                    raise ContractRefused("OBJECT_ERASED", "erased state is terminal in v1")
                if current_revision != request.expected_revision:
                    raise StateConflict("REVISION_CONFLICT", "claim expected revision is stale")
                lineage = self._claims.get(key)
                if lineage is None:
                    raise StateConflict("CLAIM_NOT_FOUND", "claim lineage missing for StateKey")
                if self._claim_is_active(lineage, now):
                    raise StateConflict("CLAIM_ACTIVE", "StateKey already has an active claim")
            except (StateConflict, ContractRefused) as exc:
                return self._persist_claim_terminal(
                    op_key=op_key,
                    action=action,
                    request=request,
                    request_digest=request_digest,
                    receipt_id=receipt_id,
                    principal_ref=principal_ref,
                    authorization_ref=authorization_ref,
                    issued_at=now,
                    exc=exc,
                )

            next_fence = lineage.highest_fence + 1
            expires_at = now + timedelta(seconds=request.ttl_seconds)
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
                fencing_token=next_fence,
                holder_ref=principal_ref,
                expires_at=expires_at,
                issued_at=now,
                reason_codes=(),
            )
            self._claims[key] = StoredClaim(
                tenant_org_id=tenant_org_id,
                scope_ref=request.scope_ref,
                object_id=request.object_id,
                highest_fence=next_fence,
                active=True,
                claim_id=claim_id,
                fencing_token=next_fence,
                holder_ref=principal_ref,
                acquired_at=now,
                expires_at=expires_at,
                released_at=None,
                guard_version=lineage.guard_version,
            )
            self._operations[op_key] = StoredOperation(
                action, request_digest, CLAIM_RECEIPT_SCHEMA_REF, receipt
            )
            return deepcopy(receipt)

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
        async with self._lock:
            op_key = self._operation_key(tenant_org_id, request.scope_ref, request.operation_id)
            existing = self._resolve_existing_operation(
                key=op_key, action=action, request_digest=request_digest
            )
            if existing is not None:
                if not isinstance(existing.receipt, StateClaimReceipt):
                    raise StateConflict(
                        "IDEMPOTENCY_COLLISION", "receipt family does not match action"
                    )
                return deepcopy(existing.receipt)
            budget.require_mutation_start(
                operation_id=request.operation_id, scope_ref=request.scope_ref
            )
            key = self._object_key(tenant_org_id, request.scope_ref, request.object_id)
            now = self._now()
            try:
                lineage = self._claims.get(key)
                if lineage is None:
                    raise StateConflict("CLAIM_NOT_FOUND", "claim lineage missing for StateKey")
                self._assert_supplied_claim(
                    lineage,
                    claim_id=request.claim_id,
                    fencing_token=request.fencing_token,
                    holder_ref=principal_ref,
                    now=now,
                )
            except (StateConflict, ContractRefused) as exc:
                return self._persist_claim_terminal(
                    op_key=op_key,
                    action=action,
                    request=request,
                    request_digest=request_digest,
                    receipt_id=receipt_id,
                    principal_ref=principal_ref,
                    authorization_ref=authorization_ref,
                    issued_at=now,
                    exc=exc,
                )

            expires_at = now + timedelta(seconds=request.ttl_seconds)
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
                expires_at=expires_at,
                issued_at=now,
                reason_codes=(),
            )
            self._claims[key] = StoredClaim(
                tenant_org_id=lineage.tenant_org_id,
                scope_ref=lineage.scope_ref,
                object_id=lineage.object_id,
                highest_fence=lineage.highest_fence,
                active=True,
                claim_id=lineage.claim_id,
                fencing_token=lineage.fencing_token,
                holder_ref=lineage.holder_ref,
                acquired_at=lineage.acquired_at,
                expires_at=expires_at,
                released_at=None,
                guard_version=lineage.guard_version,
            )
            self._operations[op_key] = StoredOperation(
                action, request_digest, CLAIM_RECEIPT_SCHEMA_REF, receipt
            )
            return deepcopy(receipt)

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
        async with self._lock:
            op_key = self._operation_key(tenant_org_id, request.scope_ref, request.operation_id)
            existing = self._resolve_existing_operation(
                key=op_key, action=action, request_digest=request_digest
            )
            if existing is not None:
                if not isinstance(existing.receipt, StateClaimReceipt):
                    raise StateConflict(
                        "IDEMPOTENCY_COLLISION", "receipt family does not match action"
                    )
                return deepcopy(existing.receipt)
            budget.require_mutation_start(
                operation_id=request.operation_id, scope_ref=request.scope_ref
            )
            key = self._object_key(tenant_org_id, request.scope_ref, request.object_id)
            now = self._now()
            try:
                lineage = self._claims.get(key)
                if lineage is None:
                    raise StateConflict("CLAIM_NOT_FOUND", "claim lineage missing for StateKey")
                self._assert_supplied_claim(
                    lineage,
                    claim_id=request.claim_id,
                    fencing_token=request.fencing_token,
                    holder_ref=principal_ref,
                    now=now,
                )
            except (StateConflict, ContractRefused) as exc:
                return self._persist_claim_terminal(
                    op_key=op_key,
                    action=action,
                    request=request,
                    request_digest=request_digest,
                    receipt_id=receipt_id,
                    principal_ref=principal_ref,
                    authorization_ref=authorization_ref,
                    issued_at=now,
                    exc=exc,
                )

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
                released_at=now,
                issued_at=now,
                reason_codes=(),
            )
            self._claims[key] = StoredClaim(
                tenant_org_id=lineage.tenant_org_id,
                scope_ref=lineage.scope_ref,
                object_id=lineage.object_id,
                highest_fence=lineage.highest_fence,
                active=False,
                claim_id=lineage.claim_id,
                fencing_token=lineage.fencing_token,
                holder_ref=lineage.holder_ref,
                acquired_at=lineage.acquired_at,
                expires_at=lineage.expires_at,
                released_at=now,
                guard_version=lineage.guard_version,
            )
            self._operations[op_key] = StoredOperation(
                action, request_digest, CLAIM_RECEIPT_SCHEMA_REF, receipt
            )
            return deepcopy(receipt)

    async def current_journal_seq(
        self, tenant_org_id: str, scope_ref: str, *, budget: ExecutionBudget | None = None
    ) -> int:
        async with self._lock:
            return int(self._scope_seq[(tenant_org_id, scope_ref)])

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
        async with self._lock:
            allowed = set(schema_refs)
            rows: list[StateRef] = []
            for (tenant, scope, object_id), revision in sorted(
                self._current.items(), key=lambda item: item[0][2]
            ):
                if tenant != tenant_org_id or scope != scope_ref:
                    continue
                if after_object_id is not None and object_id <= after_object_id:
                    continue
                stored = self._revisions[(tenant, scope, object_id, revision)]
                ref = stored.state_ref
                if ref.schema_ref not in allowed or ref.lifecycle != lifecycle:
                    continue
                rows.append(deepcopy(ref))
                if len(rows) >= limit + 1:
                    break
            has_more = len(rows) > limit
            return tuple(rows[:limit]), has_more

    async def read_events(
        self,
        tenant_org_id: str,
        scope_ref: str,
        *,
        after_seq: int,
        limit: int,
        budget: ExecutionBudget | None = None,
    ) -> EventSlice:
        async with self._lock:
            events = list(self._events[(tenant_org_id, scope_ref)])
            high_water = int(self._scope_seq[(tenant_org_id, scope_ref)])
            retained_from = events[0].journal_seq if events else high_water + 1
            selected = [deepcopy(event) for event in events if event.journal_seq > after_seq][
                :limit
            ]
            return EventSlice(tuple(selected), high_water, retained_from)

    async def get_consumer_cursor(
        self,
        tenant_org_id: str,
        scope_ref: str,
        consumer_ref: str,
        *,
        budget: ExecutionBudget | None = None,
    ) -> int | None:
        async with self._lock:
            return self._consumer_cursors.get((tenant_org_id, scope_ref, consumer_ref))

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
        async with self._lock:
            op_key = self._operation_key(tenant_org_id, scope_ref, operation.receipt.operation_id)
            existing = self._resolve_existing_operation(
                key=op_key, action=operation.action, request_digest=operation.request_digest
            )
            if existing is not None:
                if not isinstance(existing.receipt, StateAckReceipt):
                    raise StateConflict(
                        "IDEMPOTENCY_COLLISION", "receipt family does not match action"
                    )
                return deepcopy(existing.receipt)
            budget.require_mutation_start(
                operation_id=operation.receipt.operation_id, scope_ref=scope_ref
            )
            key = (tenant_org_id, scope_ref, consumer_ref)
            current = self._consumer_cursors.get(key)
            if current is not None and cursor_seq < current:
                raise StateConflict("ACK_REGRESSION", "consumer cursor cannot move backward")
            if not isinstance(operation.receipt, StateAckReceipt):
                raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")
            receipt = operation.receipt.model_copy(update={"committed_cursor": committed_cursor})
            self._consumer_cursors[key] = cursor_seq
            self._operations[op_key] = StoredOperation(
                operation.action, operation.request_digest, ACK_RECEIPT_SCHEMA_REF, receipt
            )
            return deepcopy(receipt)

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
        async with self._lock:
            key = self._object_key(tenant_org_id, scope_ref, object_id)
            latest = self._current.get(key)
            if latest is None:
                return None
            events = [
                deepcopy(event)
                for event in self._events[(tenant_org_id, scope_ref)]
                if event.state_ref.object_id == object_id
            ]
            events.sort(key=lambda event: event.state_ref.revision)
            oldest = events[0].state_ref.revision if events else latest
            selected = [event for event in events if event.state_ref.revision > after_revision][
                :limit
            ]
            return HistorySlice(tuple(selected), oldest, latest)

    async def _trim_events_before_for_test(
        self, tenant_org_id: str, scope_ref: str, journal_seq: int
    ) -> None:
        """Conformance-only helper to simulate journal retention before STATE-04."""
        async with self._lock:
            self._events[(tenant_org_id, scope_ref)] = [
                event
                for event in self._events[(tenant_org_id, scope_ref)]
                if event.journal_seq >= journal_seq
            ]
