from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from .cursor import CursorClaims, CursorCodec, CursorError, filter_digest
from .digests import request_digest, state_digest
from .errors import ContractRefused, StateConflict, StateInfrastructureFailure
from .execution import ExecutionBudget
from .models import (
    StateAckReceipt,
    StateAckRequest,
    StateClaimReceipt,
    StateClaimRequest,
    StateCreateRequest,
    StateEventPage,
    StateEventReadRequest,
    StateHistoryPage,
    StateHistoryRequest,
    StateListPage,
    StateListRequest,
    StateOperationInspectRequest,
    StateOperationInspectResult,
    StateProblem,
    StateReadRequest,
    StateReadResult,
    StateReceipt,
    StateRef,
    StateReleaseRequest,
    StateRenewRequest,
    StateRestoreRequest,
    StateTombstoneRequest,
    StateTransitionEvent,
    StateTransitionRequest,
)
from .ports import (
    ACK_RECEIPT_SCHEMA_REF,
    STATE_RECEIPT_SCHEMA_REF,
    ContractCatalogPort,
    PendingRevision,
    ScopeAuthorizationDecision,
    ScopeAuthorizationPort,
    ScopeAuthorizationRequest,
    StateStorePort,
    StoredOperation,
    StoredRevision,
)


@dataclass(frozen=True)
class CallerContext:
    tenant_org_id: str
    principal_ref: str
    authorization_ref: str
    execution_budget: ExecutionBudget
    causation_ref: str | None = None
    on_behalf_of_ref: str | None = None
    originator_ref: str | None = None
    delegation_evidence_refs: tuple[str, ...] = ()


class StateService:
    def __init__(
        self,
        store: StateStorePort,
        catalog: ContractCatalogPort,
        *,
        cursor_codec: CursorCodec,
        authorization: ScopeAuthorizationPort | None = None,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._cursors = cursor_codec
        self._authorization = authorization

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _problem(
        exc: StateInfrastructureFailure, *, effect: Literal["read", "mutation"]
    ) -> StateProblem:
        if effect == "read":
            retry_class: Literal["later", "same_operation", "inspect_then_same_operation"] = "later"
        elif exc.code == "OUTCOME_UNKNOWN":
            retry_class = "inspect_then_same_operation"
        else:
            retry_class = "same_operation"
        fields: dict[str, object] = {
            "contract_version": "1.0",
            "status": "failed",
            "code": exc.code,
            "retry_class": retry_class,
            "reason_codes": (exc.code,),
        }
        if exc.operation_id is not None:
            fields["operation_id"] = exc.operation_id
        if exc.scope_ref is not None:
            fields["scope_ref"] = exc.scope_ref
        return StateProblem.model_validate(fields)

    async def _authorize(
        self,
        caller: CallerContext,
        *,
        scope_ref: str,
        action: str,
        effect: Literal["read", "mutation"],
        consumer_ref: str | None = None,
    ) -> CallerContext | StateProblem:
        # Tenant membership is an explicit v1 base authorization decision.  A
        # configured ScopeAuthorizationPort may only tighten/extend that boundary;
        # identity by itself never grants a delegated consumer identity.
        if self._authorization is None:
            if consumer_ref is not None and consumer_ref != caller.principal_ref:
                return self._coded_problem(
                    code="SCOPE_FORBIDDEN", retry_class="no", scope_ref=scope_ref
                )
            return caller
        request = ScopeAuthorizationRequest(
            tenant_org_id=caller.tenant_org_id,
            principal_ref=caller.principal_ref,
            scope_ref=scope_ref,
            action=action,
            base_authorization_ref=caller.authorization_ref,
            on_behalf_of_ref=caller.on_behalf_of_ref,
            originator_ref=caller.originator_ref,
            delegation_evidence_refs=caller.delegation_evidence_refs,
            consumer_ref=consumer_ref,
        )
        try:
            decision: ScopeAuthorizationDecision = await self._authorization.authorize(request)
        except StateInfrastructureFailure as exc:
            return self._problem(exc, effect=effect)
        if not decision.allowed or not decision.authorization_ref.strip():
            return self._coded_problem(
                code=decision.reason_code or "SCOPE_FORBIDDEN",
                retry_class="no",
                scope_ref=scope_ref,
            )
        return replace(caller, authorization_ref=decision.authorization_ref)

    @staticmethod
    def _idempotency_collision_problem(*, operation_id: str, scope_ref: str) -> StateProblem:
        return StateProblem(
            contract_version="1.0",
            status="failed",
            code="IDEMPOTENCY_COLLISION",
            retry_class="no",
            operation_id=operation_id,
            scope_ref=scope_ref,
            reason_codes=("IDEMPOTENCY_COLLISION",),
        )

    @staticmethod
    def _coded_problem(
        *,
        code: str,
        retry_class: Literal["no", "cold_resync", "later"],
        scope_ref: str | None = None,
        operation_id: str | None = None,
    ) -> StateProblem:
        fields: dict[str, object] = {
            "contract_version": "1.0",
            "status": "failed",
            "code": code,
            "retry_class": retry_class,
            "reason_codes": (code,),
        }
        if scope_ref is not None:
            fields["scope_ref"] = scope_ref
        if operation_id is not None:
            fields["operation_id"] = operation_id
        return StateProblem.model_validate(fields)

    def _decode_cursor(
        self,
        cursor: str,
        *,
        purpose: Literal["list", "events", "history"],
        caller: CallerContext,
        scope_ref: str,
        consumer_ref: str | None = None,
        object_id: str | None = None,
    ) -> CursorClaims | StateProblem:
        try:
            claims = self._cursors.decode(cursor, purpose=purpose)
        except CursorError as exc:
            return self._coded_problem(code=exc.code, retry_class="no", scope_ref=scope_ref)
        expected_scope_binding = self._cursors.scope_binding(caller.tenant_org_id, scope_ref)
        if claims.scope_binding != expected_scope_binding:
            return self._coded_problem(
                code="CURSOR_SCOPE_MISMATCH", retry_class="no", scope_ref=scope_ref
            )
        if consumer_ref is not None:
            expected_consumer_binding = self._cursors.consumer_binding(consumer_ref)
            if claims.consumer_binding != expected_consumer_binding:
                return self._coded_problem(
                    code="CURSOR_CONSUMER_MISMATCH", retry_class="no", scope_ref=scope_ref
                )
        if object_id is not None:
            expected_object_binding = self._cursors.object_binding(object_id)
            if claims.object_binding != expected_object_binding:
                return self._coded_problem(
                    code="CURSOR_INVALID", retry_class="no", scope_ref=scope_ref
                )
        return claims

    async def _existing_operation(
        self, *, caller: CallerContext, scope_ref: str, operation_id: str, action: str, digest: str
    ) -> StoredOperation | StateProblem | None:
        try:
            existing = await self._store.get_operation(
                caller.tenant_org_id,
                scope_ref,
                operation_id,
                budget=caller.execution_budget,
            )
        except StateInfrastructureFailure as exc:
            return self._problem(exc, effect="mutation")
        if existing is None:
            return None
        if existing.action == action and existing.request_digest == digest:
            return existing
        return self._idempotency_collision_problem(operation_id=operation_id, scope_ref=scope_ref)

    async def _record_state_refusal(
        self,
        *,
        caller: CallerContext,
        action: Literal["state.create", "state.transition"],
        operation_id: str,
        request_digest_value: str,
        scope_ref: str,
        receipt_id: str,
        issued_at: datetime,
        exc: ContractRefused,
    ) -> StateReceipt | StateProblem:
        receipt = StateReceipt(
            contract_version="1.0",
            receipt_id=receipt_id,
            action=action,
            operation_id=operation_id,
            request_digest=request_digest_value,
            scope_ref=scope_ref,
            principal_ref=caller.principal_ref,
            authorization_ref=caller.authorization_ref,
            status="refused",
            issued_at=issued_at,
            reason_codes=(exc.code,),
        )
        operation = StoredOperation(action, request_digest_value, STATE_RECEIPT_SCHEMA_REF, receipt)
        try:
            resolved = await self._store.record_operation(
                tenant_org_id=caller.tenant_org_id,
                scope_ref=scope_ref,
                operation=operation,
                budget=caller.execution_budget,
            )
        except StateInfrastructureFailure as infra:
            return self._problem(infra, effect="mutation")
        except StateConflict as collision:
            if collision.code != "IDEMPOTENCY_COLLISION":
                raise
            return self._idempotency_collision_problem(
                operation_id=operation_id, scope_ref=scope_ref
            )
        if not isinstance(resolved, StateReceipt):
            raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")
        return resolved

    async def create(
        self, caller: CallerContext, request: StateCreateRequest
    ) -> StateReceipt | StateProblem:
        action: Literal["state.create"] = "state.create"
        authorized = await self._authorize(
            caller, scope_ref=request.scope_ref, action=action, effect="mutation"
        )
        if isinstance(authorized, StateProblem):
            return authorized
        caller = authorized
        rd = request_digest(action, request.model_dump(mode="json"))
        existing = await self._existing_operation(
            caller=caller,
            scope_ref=request.scope_ref,
            operation_id=request.operation_id,
            action=action,
            digest=rd,
        )
        if isinstance(existing, StateProblem):
            return existing
        if existing is not None:
            if not isinstance(existing.receipt, StateReceipt):
                raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")
            return existing.receipt

        now = self._now()
        receipt_id = f"receipt.{uuid4().hex}"
        try:
            await self._catalog.validate_state_payload(
                request.schema_ref, request.schema_digest, request.payload
            )
            await self._catalog.assert_retention_contract(
                request.retention_policy_ref, request.retention_policy_digest
            )
        except ContractRefused as exc:
            return await self._record_state_refusal(
                caller=caller,
                action=action,
                operation_id=request.operation_id,
                request_digest_value=rd,
                scope_ref=request.scope_ref,
                receipt_id=receipt_id,
                issued_at=now,
                exc=exc,
            )

        digest = state_digest(
            schema_ref=request.schema_ref,
            schema_digest=request.schema_digest,
            lifecycle="active",
            payload=request.payload,
        )
        state_ref = StateRef(
            contract_version="1.0",
            object_id=request.object_id,
            scope_ref=request.scope_ref,
            revision=0,
            state_digest=digest,
            schema_ref=request.schema_ref,
            schema_digest=request.schema_digest,
            lifecycle="active",
        )
        event_id = f"event.{uuid4().hex}"
        receipt = StateReceipt(
            contract_version="1.0",
            receipt_id=receipt_id,
            action=action,
            operation_id=request.operation_id,
            request_digest=rd,
            scope_ref=request.scope_ref,
            principal_ref=caller.principal_ref,
            authorization_ref=caller.authorization_ref,
            status="committed",
            state_ref=state_ref,
            event_ref=event_id,
            issued_at=now,
            reason_codes=(),
        )
        event = StateTransitionEvent(
            contract_version="1.0",
            event_id=event_id,
            journal_seq=1,
            event_kind="created",
            state_ref=state_ref,
            prior_revision=None,
            operation_id=request.operation_id,
            principal_ref=caller.principal_ref,
            committed_at=now,
            **({"causation_ref": caller.causation_ref} if caller.causation_ref is not None else {}),
        )
        revision = StoredRevision(
            caller.tenant_org_id,
            state_ref,
            dict(request.payload),
            now,
            request.retention_policy_ref,
            request.retention_policy_digest,
        )
        try:
            return await self._store.create(
                revision=revision,
                operation=StoredOperation(action, rd, STATE_RECEIPT_SCHEMA_REF, receipt),
                event=event,
                budget=caller.execution_budget,
            )
        except StateInfrastructureFailure as exc:
            return self._problem(exc, effect="mutation")
        except StateConflict as collision:
            if collision.code != "IDEMPOTENCY_COLLISION":
                raise
            return self._idempotency_collision_problem(
                operation_id=request.operation_id, scope_ref=request.scope_ref
            )

    async def get(
        self, caller: CallerContext, request: StateReadRequest
    ) -> StateReadResult | StateProblem:
        authorized = await self._authorize(
            caller, scope_ref=request.scope_ref, action="state.get", effect="read"
        )
        if isinstance(authorized, StateProblem):
            return authorized
        caller = authorized
        try:
            stored = await self._store.get_revision(
                caller.tenant_org_id,
                request.scope_ref,
                request.object_id,
                request.revision,
                budget=caller.execution_budget,
            )
        except StateInfrastructureFailure as exc:
            return self._problem(exc, effect="read")
        if stored is None:
            return self._coded_problem(
                code="OBJECT_NOT_FOUND", retry_class="no", scope_ref=request.scope_ref
            )
        if request.revision is None and stored.state_ref.lifecycle == "tombstoned":
            # Ordinary current-state reads do not surface tombstoned objects.
            return self._coded_problem(
                code="OBJECT_NOT_FOUND", retry_class="no", scope_ref=request.scope_ref
            )
        fields: dict[str, object] = {
            "contract_version": "1.0",
            "state_ref": stored.state_ref,
            "payload_status": "erased" if stored.payload is None else "present",
            "recorded_at": stored.recorded_at,
            "observed_at": self._now(),
        }
        if stored.payload is not None:
            fields["payload"] = stored.payload
        return StateReadResult.model_validate(fields)

    async def transition(
        self, caller: CallerContext, request: StateTransitionRequest
    ) -> StateReceipt | StateProblem:
        action: Literal["state.transition"] = "state.transition"
        authorized = await self._authorize(
            caller, scope_ref=request.scope_ref, action=action, effect="mutation"
        )
        if isinstance(authorized, StateProblem):
            return authorized
        caller = authorized
        rd = request_digest(action, request.model_dump(mode="json", exclude_none=True))
        existing = await self._existing_operation(
            caller=caller,
            scope_ref=request.scope_ref,
            operation_id=request.operation_id,
            action=action,
            digest=rd,
        )
        if isinstance(existing, StateProblem):
            return existing
        if existing is not None:
            if not isinstance(existing.receipt, StateReceipt):
                raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")
            return existing.receipt

        now = self._now()
        receipt_id = f"receipt.{uuid4().hex}"
        try:
            await self._catalog.validate_state_payload(
                request.expected_schema_ref, request.expected_schema_digest, request.payload
            )
        except ContractRefused as exc:
            return await self._record_state_refusal(
                caller=caller,
                action=action,
                operation_id=request.operation_id,
                request_digest_value=rd,
                scope_ref=request.scope_ref,
                receipt_id=receipt_id,
                issued_at=now,
                exc=exc,
            )

        new_digest = state_digest(
            schema_ref=request.expected_schema_ref,
            schema_digest=request.expected_schema_digest,
            lifecycle="active",
            payload=request.payload,
        )
        state_ref = StateRef(
            contract_version="1.0",
            object_id=request.object_id,
            scope_ref=request.scope_ref,
            revision=request.expected_revision + 1,
            state_digest=new_digest,
            schema_ref=request.expected_schema_ref,
            schema_digest=request.expected_schema_digest,
            lifecycle="active",
        )
        event_id = f"event.{uuid4().hex}"
        receipt = StateReceipt(
            contract_version="1.0",
            receipt_id=receipt_id,
            action=action,
            operation_id=request.operation_id,
            request_digest=rd,
            scope_ref=request.scope_ref,
            principal_ref=caller.principal_ref,
            authorization_ref=caller.authorization_ref,
            status="committed",
            state_ref=state_ref,
            event_ref=event_id,
            issued_at=now,
            reason_codes=(),
        )
        event = StateTransitionEvent(
            contract_version="1.0",
            event_id=event_id,
            journal_seq=1,
            event_kind="transitioned",
            state_ref=state_ref,
            prior_revision=request.expected_revision,
            transition_label=request.transition_label,
            operation_id=request.operation_id,
            principal_ref=caller.principal_ref,
            committed_at=now,
            **({"causation_ref": caller.causation_ref} if caller.causation_ref is not None else {}),
        )
        revision = PendingRevision(
            caller.tenant_org_id,
            state_ref,
            dict(request.payload),
            now,
        )
        try:
            return await self._store.transition(
                revision=revision,
                expected_revision=request.expected_revision,
                expected_state_digest=request.expected_state_digest,
                expected_schema_ref=request.expected_schema_ref,
                expected_schema_digest=request.expected_schema_digest,
                claim_id=request.claim_id,
                fencing_token=request.fencing_token,
                holder_ref=caller.principal_ref,
                operation=StoredOperation(action, rd, STATE_RECEIPT_SCHEMA_REF, receipt),
                event=event,
                budget=caller.execution_budget,
            )
        except StateInfrastructureFailure as exc:
            return self._problem(exc, effect="mutation")
        except StateConflict as collision:
            if collision.code != "IDEMPOTENCY_COLLISION":
                raise
            return self._idempotency_collision_problem(
                operation_id=request.operation_id, scope_ref=request.scope_ref
            )

    async def tombstone(
        self, caller: CallerContext, request: StateTombstoneRequest
    ) -> StateReceipt | StateProblem:
        action: Literal["state.tombstone"] = "state.tombstone"
        authorized = await self._authorize(
            caller, scope_ref=request.scope_ref, action=action, effect="mutation"
        )
        if isinstance(authorized, StateProblem):
            return authorized
        caller = authorized
        rd = request_digest(action, request.model_dump(mode="json", exclude_none=True))
        existing = await self._existing_operation(
            caller=caller,
            scope_ref=request.scope_ref,
            operation_id=request.operation_id,
            action=action,
            digest=rd,
        )
        if isinstance(existing, StateProblem):
            return existing
        if existing is not None:
            if not isinstance(existing.receipt, StateReceipt):
                raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")
            return existing.receipt
        try:
            return await self._store.tombstone(
                tenant_org_id=caller.tenant_org_id,
                request=request,
                request_digest=rd,
                receipt_id=f"receipt.{uuid4().hex}",
                event_id=f"event.{uuid4().hex}",
                principal_ref=caller.principal_ref,
                authorization_ref=caller.authorization_ref,
                causation_ref=caller.causation_ref,
                budget=caller.execution_budget,
            )
        except StateInfrastructureFailure as exc:
            return self._problem(exc, effect="mutation")
        except StateConflict as collision:
            if collision.code != "IDEMPOTENCY_COLLISION":
                raise
            return self._idempotency_collision_problem(
                operation_id=request.operation_id, scope_ref=request.scope_ref
            )

    async def restore(
        self, caller: CallerContext, request: StateRestoreRequest
    ) -> StateReceipt | StateProblem:
        action: Literal["state.restore"] = "state.restore"
        authorized = await self._authorize(
            caller, scope_ref=request.scope_ref, action=action, effect="mutation"
        )
        if isinstance(authorized, StateProblem):
            return authorized
        caller = authorized
        rd = request_digest(action, request.model_dump(mode="json", exclude_none=True))
        existing = await self._existing_operation(
            caller=caller,
            scope_ref=request.scope_ref,
            operation_id=request.operation_id,
            action=action,
            digest=rd,
        )
        if isinstance(existing, StateProblem):
            return existing
        if existing is not None:
            if not isinstance(existing.receipt, StateReceipt):
                raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")
            return existing.receipt
        try:
            return await self._store.restore(
                tenant_org_id=caller.tenant_org_id,
                request=request,
                request_digest=rd,
                receipt_id=f"receipt.{uuid4().hex}",
                event_id=f"event.{uuid4().hex}",
                principal_ref=caller.principal_ref,
                authorization_ref=caller.authorization_ref,
                causation_ref=caller.causation_ref,
                budget=caller.execution_budget,
            )
        except StateInfrastructureFailure as exc:
            return self._problem(exc, effect="mutation")
        except StateConflict as collision:
            if collision.code != "IDEMPOTENCY_COLLISION":
                raise
            return self._idempotency_collision_problem(
                operation_id=request.operation_id, scope_ref=request.scope_ref
            )

    async def claim(
        self, caller: CallerContext, request: StateClaimRequest
    ) -> StateClaimReceipt | StateProblem:
        action: Literal["state.claim"] = "state.claim"
        authorized = await self._authorize(
            caller, scope_ref=request.scope_ref, action=action, effect="mutation"
        )
        if isinstance(authorized, StateProblem):
            return authorized
        caller = authorized
        rd = request_digest(action, request.model_dump(mode="json"))
        existing = await self._existing_operation(
            caller=caller,
            scope_ref=request.scope_ref,
            operation_id=request.operation_id,
            action=action,
            digest=rd,
        )
        if isinstance(existing, StateProblem):
            return existing
        if existing is not None:
            if not isinstance(existing.receipt, StateClaimReceipt):
                raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")
            return existing.receipt
        try:
            return await self._store.claim(
                tenant_org_id=caller.tenant_org_id,
                request=request,
                request_digest=rd,
                receipt_id=f"receipt.{uuid4().hex}",
                claim_id=f"claim.{uuid4().hex}",
                principal_ref=caller.principal_ref,
                authorization_ref=caller.authorization_ref,
                budget=caller.execution_budget,
            )
        except StateInfrastructureFailure as exc:
            return self._problem(exc, effect="mutation")
        except StateConflict as collision:
            if collision.code != "IDEMPOTENCY_COLLISION":
                raise
            return self._idempotency_collision_problem(
                operation_id=request.operation_id, scope_ref=request.scope_ref
            )

    async def renew(
        self, caller: CallerContext, request: StateRenewRequest
    ) -> StateClaimReceipt | StateProblem:
        action: Literal["state.renew"] = "state.renew"
        authorized = await self._authorize(
            caller, scope_ref=request.scope_ref, action=action, effect="mutation"
        )
        if isinstance(authorized, StateProblem):
            return authorized
        caller = authorized
        rd = request_digest(action, request.model_dump(mode="json"))
        existing = await self._existing_operation(
            caller=caller,
            scope_ref=request.scope_ref,
            operation_id=request.operation_id,
            action=action,
            digest=rd,
        )
        if isinstance(existing, StateProblem):
            return existing
        if existing is not None:
            if not isinstance(existing.receipt, StateClaimReceipt):
                raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")
            return existing.receipt
        try:
            return await self._store.renew(
                tenant_org_id=caller.tenant_org_id,
                request=request,
                request_digest=rd,
                receipt_id=f"receipt.{uuid4().hex}",
                principal_ref=caller.principal_ref,
                authorization_ref=caller.authorization_ref,
                budget=caller.execution_budget,
            )
        except StateInfrastructureFailure as exc:
            return self._problem(exc, effect="mutation")
        except StateConflict as collision:
            if collision.code != "IDEMPOTENCY_COLLISION":
                raise
            return self._idempotency_collision_problem(
                operation_id=request.operation_id, scope_ref=request.scope_ref
            )

    async def release(
        self, caller: CallerContext, request: StateReleaseRequest
    ) -> StateClaimReceipt | StateProblem:
        action: Literal["state.release"] = "state.release"
        authorized = await self._authorize(
            caller, scope_ref=request.scope_ref, action=action, effect="mutation"
        )
        if isinstance(authorized, StateProblem):
            return authorized
        caller = authorized
        rd = request_digest(action, request.model_dump(mode="json"))
        existing = await self._existing_operation(
            caller=caller,
            scope_ref=request.scope_ref,
            operation_id=request.operation_id,
            action=action,
            digest=rd,
        )
        if isinstance(existing, StateProblem):
            return existing
        if existing is not None:
            if not isinstance(existing.receipt, StateClaimReceipt):
                raise StateConflict("IDEMPOTENCY_COLLISION", "receipt family does not match action")
            return existing.receipt
        try:
            return await self._store.release(
                tenant_org_id=caller.tenant_org_id,
                request=request,
                request_digest=rd,
                receipt_id=f"receipt.{uuid4().hex}",
                principal_ref=caller.principal_ref,
                authorization_ref=caller.authorization_ref,
                budget=caller.execution_budget,
            )
        except StateInfrastructureFailure as exc:
            return self._problem(exc, effect="mutation")
        except StateConflict as collision:
            if collision.code != "IDEMPOTENCY_COLLISION":
                raise
            return self._idempotency_collision_problem(
                operation_id=request.operation_id, scope_ref=request.scope_ref
            )

    async def list_states(
        self, caller: CallerContext, request: StateListRequest
    ) -> StateListPage | StateProblem:
        authorized = await self._authorize(
            caller,
            scope_ref=request.scope_ref,
            action="state.list",
            effect="read",
            consumer_ref=request.consumer_ref,
        )
        if isinstance(authorized, StateProblem):
            return authorized
        caller = authorized
        digest = filter_digest(schema_refs=request.schema_refs, lifecycle=request.lifecycle)
        after_object_id: str | None = None
        if request.cursor is None:
            try:
                resume_seq = await self._store.current_journal_seq(
                    caller.tenant_org_id,
                    request.scope_ref,
                    budget=caller.execution_budget,
                )
            except StateInfrastructureFailure as exc:
                return self._problem(exc, effect="read")
        else:
            decoded = self._decode_cursor(
                request.cursor,
                purpose="list",
                caller=caller,
                scope_ref=request.scope_ref,
                consumer_ref=request.consumer_ref,
            )
            if isinstance(decoded, StateProblem):
                return decoded
            if decoded.filter_digest != digest or decoded.resume_seq is None:
                return self._coded_problem(
                    code="CURSOR_INVALID", retry_class="no", scope_ref=request.scope_ref
                )
            resume_seq = decoded.resume_seq
            after_object_id = decoded.last_object_id
        try:
            states, has_more = await self._store.list_state_refs(
                caller.tenant_org_id,
                request.scope_ref,
                schema_refs=request.schema_refs,
                lifecycle=request.lifecycle,
                after_object_id=after_object_id,
                limit=request.limit,
                budget=caller.execution_budget,
            )
        except StateInfrastructureFailure as exc:
            return self._problem(exc, effect="read")
        resume_event_cursor = self._cursors.issue(
            CursorClaims(
                purpose="events",
                scope_binding=self._cursors.scope_binding(caller.tenant_org_id, request.scope_ref),
                consumer_binding=self._cursors.consumer_binding(request.consumer_ref),
                position=resume_seq,
            )
        )
        next_cursor: str | None = None
        if has_more and states:
            next_cursor = self._cursors.issue(
                CursorClaims(
                    purpose="list",
                    scope_binding=self._cursors.scope_binding(
                        caller.tenant_org_id, request.scope_ref
                    ),
                    consumer_binding=self._cursors.consumer_binding(request.consumer_ref),
                    position=0,
                    filter_digest=digest,
                    resume_seq=resume_seq,
                    last_object_id=states[-1].object_id,
                )
            )
        return StateListPage(
            contract_version="1.0",
            scope_ref=request.scope_ref,
            consumer_ref=request.consumer_ref,
            states=states,
            coverage="bounded" if has_more else "complete",
            resume_event_cursor=resume_event_cursor,
            observed_at=self._now(),
            **({"next_cursor": next_cursor} if next_cursor is not None else {}),
        )

    async def events(
        self, caller: CallerContext, request: StateEventReadRequest
    ) -> StateEventPage | StateProblem:
        authorized = await self._authorize(
            caller,
            scope_ref=request.scope_ref,
            action="state.events",
            effect="read",
            consumer_ref=request.consumer_ref,
        )
        if isinstance(authorized, StateProblem):
            return authorized
        caller = authorized
        if request.cursor is None:
            try:
                checkpoint = await self._store.get_consumer_cursor(
                    caller.tenant_org_id,
                    request.scope_ref,
                    request.consumer_ref,
                    budget=caller.execution_budget,
                )
            except StateInfrastructureFailure as exc:
                return self._problem(exc, effect="read")
            has_prior_position = checkpoint is not None
            after_seq = 0 if checkpoint is None else checkpoint
        else:
            has_prior_position = True
            decoded = self._decode_cursor(
                request.cursor,
                purpose="events",
                caller=caller,
                scope_ref=request.scope_ref,
                consumer_ref=request.consumer_ref,
            )
            if isinstance(decoded, StateProblem):
                return decoded
            after_seq = decoded.position
        try:
            page = await self._store.read_events(
                caller.tenant_org_id,
                request.scope_ref,
                after_seq=after_seq,
                limit=request.limit,
                budget=caller.execution_budget,
            )
        except StateInfrastructureFailure as exc:
            return self._problem(exc, effect="read")
        if after_seq > page.high_water_seq:
            return self._coded_problem(
                code="CURSOR_INVALID", retry_class="no", scope_ref=request.scope_ref
            )
        if after_seq < page.retained_from_seq - 1:
            if has_prior_position:
                return StateEventPage(
                    contract_version="1.0",
                    scope_ref=request.scope_ref,
                    consumer_ref=request.consumer_ref,
                    events=(),
                    coverage="resync_required",
                    retained_from_seq=page.retained_from_seq,
                    high_water_seq=page.high_water_seq,
                    observed_at=self._now(),
                )
            # A brand-new consumer has no stale claim to preserve.  Its logical
            # starting position is immediately before the earliest retained event.
            after_seq = page.retained_from_seq - 1
        position = page.events[-1].journal_seq if page.events else after_seq
        next_cursor = self._cursors.issue(
            CursorClaims(
                purpose="events",
                scope_binding=self._cursors.scope_binding(caller.tenant_org_id, request.scope_ref),
                consumer_binding=self._cursors.consumer_binding(request.consumer_ref),
                position=position,
            )
        )
        coverage: Literal["complete", "bounded"] = (
            "bounded" if position < page.high_water_seq else "complete"
        )
        return StateEventPage(
            contract_version="1.0",
            scope_ref=request.scope_ref,
            consumer_ref=request.consumer_ref,
            events=page.events,
            next_cursor=next_cursor,
            coverage=coverage,
            high_water_seq=page.high_water_seq,
            observed_at=self._now(),
        )

    async def acknowledge(
        self, caller: CallerContext, request: StateAckRequest
    ) -> StateAckReceipt | StateProblem:
        action = "state.ack"
        authorized = await self._authorize(
            caller,
            scope_ref=request.scope_ref,
            action=action,
            effect="mutation",
            consumer_ref=request.consumer_ref,
        )
        if isinstance(authorized, StateProblem):
            return authorized
        caller = authorized
        rd = request_digest(action, request.model_dump(mode="json"))
        existing = await self._existing_operation(
            caller=caller,
            scope_ref=request.scope_ref,
            operation_id=request.operation_id,
            action=action,
            digest=rd,
        )
        if isinstance(existing, StateProblem):
            return existing
        if existing is not None:
            if not isinstance(existing.receipt, StateAckReceipt):
                return self._idempotency_collision_problem(
                    operation_id=request.operation_id, scope_ref=request.scope_ref
                )
            return existing.receipt
        decoded = self._decode_cursor(
            request.cursor,
            purpose="events",
            caller=caller,
            scope_ref=request.scope_ref,
            consumer_ref=request.consumer_ref,
        )
        if isinstance(decoded, StateProblem):
            return decoded
        try:
            high_water = await self._store.current_journal_seq(
                caller.tenant_org_id,
                request.scope_ref,
                budget=caller.execution_budget,
            )
        except StateInfrastructureFailure as exc:
            return self._problem(exc, effect="mutation")
        if decoded.position > high_water:
            return self._coded_problem(
                code="CURSOR_INVALID",
                retry_class="no",
                scope_ref=request.scope_ref,
                operation_id=request.operation_id,
            )
        now = self._now()
        receipt = StateAckReceipt(
            contract_version="1.0",
            receipt_id=f"receipt.{uuid4().hex}",
            operation_id=request.operation_id,
            request_digest=rd,
            scope_ref=request.scope_ref,
            consumer_ref=request.consumer_ref,
            principal_ref=caller.principal_ref,
            authorization_ref=caller.authorization_ref,
            status="committed",
            committed_cursor=request.cursor,
            issued_at=now,
            reason_codes=(),
        )
        operation = StoredOperation(action, rd, ACK_RECEIPT_SCHEMA_REF, receipt)
        try:
            return await self._store.acknowledge(
                tenant_org_id=caller.tenant_org_id,
                scope_ref=request.scope_ref,
                consumer_ref=request.consumer_ref,
                cursor_seq=decoded.position,
                committed_cursor=request.cursor,
                operation=operation,
                budget=caller.execution_budget,
            )
        except StateInfrastructureFailure as exc:
            return self._problem(exc, effect="mutation")
        except StateConflict as exc:
            if exc.code == "IDEMPOTENCY_COLLISION":
                return self._idempotency_collision_problem(
                    operation_id=request.operation_id, scope_ref=request.scope_ref
                )
            if exc.code == "ACK_REGRESSION":
                return self._coded_problem(
                    code="ACK_REGRESSION",
                    retry_class="no",
                    scope_ref=request.scope_ref,
                    operation_id=request.operation_id,
                )
            raise

    async def history(
        self, caller: CallerContext, request: StateHistoryRequest
    ) -> StateHistoryPage | StateProblem:
        authorized = await self._authorize(
            caller, scope_ref=request.scope_ref, action="state.history", effect="read"
        )
        if isinstance(authorized, StateProblem):
            return authorized
        caller = authorized
        after_revision = -1
        if request.cursor is not None:
            decoded = self._decode_cursor(
                request.cursor,
                purpose="history",
                caller=caller,
                scope_ref=request.scope_ref,
                object_id=request.object_id,
            )
            if isinstance(decoded, StateProblem):
                return decoded
            after_revision = decoded.position
        try:
            history = await self._store.read_history(
                caller.tenant_org_id,
                request.scope_ref,
                request.object_id,
                after_revision=after_revision,
                limit=request.limit,
                budget=caller.execution_budget,
            )
        except StateInfrastructureFailure as exc:
            return self._problem(exc, effect="read")
        if history is None:
            return self._coded_problem(
                code="OBJECT_NOT_FOUND", retry_class="no", scope_ref=request.scope_ref
            )
        if after_revision < history.oldest_available_revision - 1 or (
            not history.events and after_revision < history.latest_revision
        ):
            return StateHistoryPage(
                contract_version="1.0",
                object_id=request.object_id,
                scope_ref=request.scope_ref,
                events=(),
                coverage="history_unavailable",
                oldest_available_revision=history.oldest_available_revision,
                latest_revision=history.latest_revision,
                observed_at=self._now(),
            )
        position = history.events[-1].state_ref.revision if history.events else after_revision
        has_more = position < history.latest_revision
        next_cursor: str | None = None
        if has_more:
            next_cursor = self._cursors.issue(
                CursorClaims(
                    purpose="history",
                    scope_binding=self._cursors.scope_binding(
                        caller.tenant_org_id, request.scope_ref
                    ),
                    object_binding=self._cursors.object_binding(request.object_id),
                    position=max(0, position),
                )
            )
        return StateHistoryPage(
            contract_version="1.0",
            object_id=request.object_id,
            scope_ref=request.scope_ref,
            events=history.events,
            coverage="bounded" if has_more else "complete",
            oldest_available_revision=history.oldest_available_revision,
            latest_revision=history.latest_revision,
            observed_at=self._now(),
            **({"next_cursor": next_cursor} if next_cursor is not None else {}),
        )

    async def inspect_operation(
        self, caller: CallerContext, request: StateOperationInspectRequest
    ) -> StateOperationInspectResult | StateProblem:
        authorized = await self._authorize(
            caller,
            scope_ref=request.scope_ref,
            action="state.operation.inspect",
            effect="read",
        )
        if isinstance(authorized, StateProblem):
            return authorized
        caller = authorized
        try:
            existing = await self._store.get_operation(
                caller.tenant_org_id,
                request.scope_ref,
                request.operation_id,
                budget=caller.execution_budget,
            )
        except StateInfrastructureFailure as exc:
            return self._problem(exc, effect="read")
        if existing is None:
            return StateOperationInspectResult(
                contract_version="1.0",
                scope_ref=request.scope_ref,
                operation_id=request.operation_id,
                status="not_found",
            )
        return StateOperationInspectResult(
            contract_version="1.0",
            scope_ref=request.scope_ref,
            operation_id=request.operation_id,
            status="found",
            request_digest=existing.request_digest,
            receipt_schema_ref=existing.receipt_schema_ref,
            receipt=existing.receipt.model_dump(mode="json", exclude_none=True),
        )
