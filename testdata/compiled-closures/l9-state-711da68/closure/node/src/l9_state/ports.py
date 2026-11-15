from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .execution import ExecutionBudget
from .internal_models import HardEraseReceipt
from .models import (
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

STATE_RECEIPT_SCHEMA_REF = "urn:l9:state:StateReceipt:1.0"
CLAIM_RECEIPT_SCHEMA_REF = "urn:l9:state:StateClaimReceipt:1.0"
ACK_RECEIPT_SCHEMA_REF = "urn:l9:state:StateAckReceipt:1.0"
HARD_ERASE_RECEIPT_SCHEMA_REF = "urn:l9:state:internal:HardEraseReceipt:1.0"

MutationReceipt = StateReceipt | StateClaimReceipt | StateAckReceipt | HardEraseReceipt


@dataclass(frozen=True)
class StoredRevision:
    tenant_org_id: str
    state_ref: StateRef
    payload: dict[str, Any] | None
    recorded_at: datetime
    retention_policy_ref: str
    retention_policy_digest: str


@dataclass(frozen=True)
class PendingRevision:
    """Provider-neutral candidate revision whose retained policy is inherited on commit."""

    tenant_org_id: str
    state_ref: StateRef
    payload: dict[str, Any] | None
    recorded_at: datetime


@dataclass(frozen=True)
class StoredOperation:
    action: str
    request_digest: str
    receipt_schema_ref: str
    receipt: MutationReceipt


@dataclass(frozen=True)
class StoredClaim:
    tenant_org_id: str
    scope_ref: str
    object_id: str
    highest_fence: int
    active: bool
    claim_id: str | None = None
    fencing_token: int | None = None
    holder_ref: str | None = None
    acquired_at: datetime | None = None
    expires_at: datetime | None = None
    released_at: datetime | None = None
    guard_version: int = 0


@dataclass(frozen=True)
class ScopeAuthorizationRequest:
    """Provider-neutral current authorization query for one State scope/action."""

    tenant_org_id: str
    principal_ref: str
    scope_ref: str
    action: str
    base_authorization_ref: str
    on_behalf_of_ref: str | None = None
    originator_ref: str | None = None
    delegation_evidence_refs: tuple[str, ...] = ()
    consumer_ref: str | None = None


@dataclass(frozen=True)
class ScopeAuthorizationDecision:
    """Current policy decision. authorization_ref identifies the decision/evidence."""

    allowed: bool
    authorization_ref: str
    reason_code: str = "SCOPE_FORBIDDEN"


class ScopeAuthorizationPort(Protocol):
    async def authorize(self, request: ScopeAuthorizationRequest) -> ScopeAuthorizationDecision: ...


@dataclass(frozen=True)
class EventSlice:
    events: tuple[StateTransitionEvent, ...]
    high_water_seq: int
    retained_from_seq: int


@dataclass(frozen=True)
class HistorySlice:
    events: tuple[StateTransitionEvent, ...]
    oldest_available_revision: int
    latest_revision: int


class StateStorePort(Protocol):
    async def get_revision(
        self,
        tenant_org_id: str,
        scope_ref: str,
        object_id: str,
        revision: int | None = None,
        *,
        budget: ExecutionBudget | None = None,
    ) -> StoredRevision | None: ...

    async def get_operation(
        self,
        tenant_org_id: str,
        scope_ref: str,
        operation_id: str,
        *,
        budget: ExecutionBudget | None = None,
    ) -> StoredOperation | None: ...

    async def record_operation(
        self,
        *,
        tenant_org_id: str,
        scope_ref: str,
        operation: StoredOperation,
        budget: ExecutionBudget,
    ) -> MutationReceipt: ...

    async def create(
        self,
        *,
        revision: StoredRevision,
        operation: StoredOperation,
        event: StateTransitionEvent,
        budget: ExecutionBudget,
    ) -> StateReceipt: ...

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
    ) -> StateReceipt: ...

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
    ) -> StateReceipt: ...

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
    ) -> StateReceipt: ...

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
    ) -> HardEraseReceipt: ...

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
    ) -> StateClaimReceipt: ...

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
    ) -> StateClaimReceipt: ...

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
    ) -> StateClaimReceipt: ...

    async def current_journal_seq(
        self, tenant_org_id: str, scope_ref: str, *, budget: ExecutionBudget | None = None
    ) -> int: ...

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
    ) -> tuple[tuple[StateRef, ...], bool]: ...

    async def read_events(
        self,
        tenant_org_id: str,
        scope_ref: str,
        *,
        after_seq: int,
        limit: int,
        budget: ExecutionBudget | None = None,
    ) -> EventSlice: ...

    async def get_consumer_cursor(
        self,
        tenant_org_id: str,
        scope_ref: str,
        consumer_ref: str,
        *,
        budget: ExecutionBudget | None = None,
    ) -> int | None: ...

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
    ) -> StateAckReceipt: ...

    async def read_history(
        self,
        tenant_org_id: str,
        scope_ref: str,
        object_id: str,
        *,
        after_revision: int,
        limit: int,
        budget: ExecutionBudget | None = None,
    ) -> HistorySlice | None: ...


class ContractCatalogPort(Protocol):
    async def validate_state_payload(
        self, schema_ref: str, schema_digest: str, payload: dict[str, Any]
    ) -> str: ...

    async def assert_retention_contract(
        self, retention_policy_ref: str, retention_policy_digest: str
    ) -> str: ...
