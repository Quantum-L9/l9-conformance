from __future__ import annotations

from datetime import datetime

from .digests import state_digest
from .models import (
    StateReceipt,
    StateRef,
    StateRestoreRequest,
    StateTombstoneRequest,
    StateTransitionEvent,
)
from .ports import PendingRevision, StoredRevision


def build_tombstone_artifacts(
    *,
    current: StoredRevision,
    request: StateTombstoneRequest,
    request_digest: str,
    receipt_id: str,
    event_id: str,
    principal_ref: str,
    authorization_ref: str,
    committed_at: datetime,
    causation_ref: str | None,
) -> tuple[PendingRevision, StateReceipt, StateTransitionEvent]:
    new_ref = StateRef(
        contract_version="1.0",
        object_id=request.object_id,
        scope_ref=request.scope_ref,
        revision=current.state_ref.revision + 1,
        state_digest=state_digest(
            schema_ref=current.state_ref.schema_ref,
            schema_digest=current.state_ref.schema_digest,
            lifecycle="tombstoned",
            payload=current.payload,
        ),
        schema_ref=current.state_ref.schema_ref,
        schema_digest=current.state_ref.schema_digest,
        lifecycle="tombstoned",
    )
    receipt = StateReceipt(
        contract_version="1.0",
        receipt_id=receipt_id,
        action="state.tombstone",
        operation_id=request.operation_id,
        request_digest=request_digest,
        scope_ref=request.scope_ref,
        principal_ref=principal_ref,
        authorization_ref=authorization_ref,
        status="committed",
        state_ref=new_ref,
        event_ref=event_id,
        issued_at=committed_at,
        reason_codes=(),
    )
    event = StateTransitionEvent(
        contract_version="1.0",
        event_id=event_id,
        journal_seq=1,
        event_kind="tombstoned",
        state_ref=new_ref,
        prior_revision=current.state_ref.revision,
        operation_id=request.operation_id,
        principal_ref=principal_ref,
        committed_at=committed_at,
        **({"causation_ref": causation_ref} if causation_ref is not None else {}),
    )
    return (
        PendingRevision(
            current.tenant_org_id,
            new_ref,
            None if current.payload is None else dict(current.payload),
            committed_at,
        ),
        receipt,
        event,
    )


def build_restore_artifacts(
    *,
    current: StoredRevision,
    source: StoredRevision,
    request: StateRestoreRequest,
    request_digest: str,
    receipt_id: str,
    event_id: str,
    principal_ref: str,
    authorization_ref: str,
    committed_at: datetime,
    causation_ref: str | None,
) -> tuple[PendingRevision, StateReceipt, StateTransitionEvent]:
    if source.payload is None:
        raise ValueError("restore source payload is unavailable")
    new_ref = StateRef(
        contract_version="1.0",
        object_id=request.object_id,
        scope_ref=request.scope_ref,
        revision=current.state_ref.revision + 1,
        state_digest=state_digest(
            schema_ref=current.state_ref.schema_ref,
            schema_digest=current.state_ref.schema_digest,
            lifecycle="active",
            payload=source.payload,
        ),
        schema_ref=current.state_ref.schema_ref,
        schema_digest=current.state_ref.schema_digest,
        lifecycle="active",
    )
    receipt = StateReceipt(
        contract_version="1.0",
        receipt_id=receipt_id,
        action="state.restore",
        operation_id=request.operation_id,
        request_digest=request_digest,
        scope_ref=request.scope_ref,
        principal_ref=principal_ref,
        authorization_ref=authorization_ref,
        status="committed",
        state_ref=new_ref,
        event_ref=event_id,
        issued_at=committed_at,
        reason_codes=(),
    )
    event = StateTransitionEvent(
        contract_version="1.0",
        event_id=event_id,
        journal_seq=1,
        event_kind="restored",
        state_ref=new_ref,
        prior_revision=current.state_ref.revision,
        transition_label=request.transition_label,
        operation_id=request.operation_id,
        principal_ref=principal_ref,
        committed_at=committed_at,
        **({"causation_ref": causation_ref} if causation_ref is not None else {}),
    )
    return (
        PendingRevision(current.tenant_org_id, new_ref, dict(source.payload), committed_at),
        receipt,
        event,
    )
