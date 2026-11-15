from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

HEX64 = r"^[a-f0-9]{64}$"
IDENT = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    nullable_fields: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_for_nonnullable_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        null_fields = sorted(
            name
            for name, field_value in value.items()
            if field_value is None and name not in cls.nullable_fields
        )
        if null_fields:
            rendered = ", ".join(null_fields)
            raise ValueError(f"explicit null is not permitted for field(s): {rendered}")
        return value

    def wire_dump(self) -> dict[str, Any]:
        """Public wire form: absent optionals omitted, declared nullable fields kept as null.

        `exclude_none` alone would drop a required-nullable field such as
        `StateTransitionEvent.prior_revision`, which the public schema requires.
        """
        return _restore_nullable(self, self.model_dump(mode="json", exclude_none=True))


def _restore_nullable(model: BaseModel, data: dict[str, Any]) -> dict[str, Any]:
    nullable: frozenset[str] = getattr(model, "nullable_fields", frozenset())
    for name in type(model).model_fields:
        value = getattr(model, name)
        if value is None:
            if name in nullable:
                data[name] = None
        elif isinstance(value, BaseModel) and isinstance(data.get(name), dict):
            _restore_nullable(value, data[name])
        elif isinstance(value, (tuple, list)) and isinstance(data.get(name), list):
            for item, dumped in zip(value, data[name], strict=True):
                if isinstance(item, BaseModel) and isinstance(dumped, dict):
                    _restore_nullable(item, dumped)
    return data


class StateRef(StrictModel):
    contract_version: Literal["1.0"]
    object_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    scope_ref: str = Field(min_length=1, max_length=512)
    revision: int = Field(ge=0)
    state_digest: str = Field(pattern=HEX64)
    schema_ref: str = Field(min_length=1, max_length=512)
    schema_digest: str = Field(pattern=HEX64)
    lifecycle: Literal["active", "tombstoned", "erased"]


class StateCreateRequest(StrictModel):
    contract_version: Literal["1.0"]
    object_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    scope_ref: str = Field(min_length=1, max_length=512)
    schema_ref: str = Field(min_length=1, max_length=512)
    schema_digest: str = Field(pattern=HEX64)
    payload: dict[str, Any] = Field(max_length=1000)
    operation_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    retention_policy_ref: str = Field(min_length=1, max_length=512)
    retention_policy_digest: str = Field(pattern=HEX64)


class StateReadRequest(StrictModel):
    contract_version: Literal["1.0"]
    object_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    scope_ref: str = Field(min_length=1, max_length=512)
    revision: int | None = Field(default=None, ge=0)


class StateTransitionRequest(StrictModel):
    contract_version: Literal["1.0"]
    object_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    scope_ref: str = Field(min_length=1, max_length=512)
    expected_revision: int = Field(ge=0)
    expected_state_digest: str = Field(pattern=HEX64)
    expected_schema_ref: str = Field(min_length=1, max_length=512)
    expected_schema_digest: str = Field(pattern=HEX64)
    payload: dict[str, Any] = Field(max_length=1000)
    transition_label: str = Field(min_length=1, max_length=200, pattern=IDENT)
    operation_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    claim_id: str | None = Field(default=None, min_length=1, max_length=200, pattern=IDENT)
    fencing_token: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def claim_pair(self) -> StateTransitionRequest:
        if (self.claim_id is None) != (self.fencing_token is None):
            raise ValueError("claim_id and fencing_token must be supplied together")
        return self


class StateTombstoneRequest(StrictModel):
    contract_version: Literal["1.0"]
    object_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    scope_ref: str = Field(min_length=1, max_length=512)
    expected_revision: int = Field(ge=0)
    expected_state_digest: str = Field(pattern=HEX64)
    operation_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    reason_code: str = Field(min_length=1, max_length=200, pattern=IDENT)
    claim_id: str | None = Field(default=None, min_length=1, max_length=200, pattern=IDENT)
    fencing_token: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def claim_pair(self) -> StateTombstoneRequest:
        if (self.claim_id is None) != (self.fencing_token is None):
            raise ValueError("claim_id and fencing_token must be supplied together")
        return self


class StateRestoreRequest(StrictModel):
    contract_version: Literal["1.0"]
    object_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    scope_ref: str = Field(min_length=1, max_length=512)
    expected_revision: int = Field(ge=0)
    expected_state_digest: str = Field(pattern=HEX64)
    source_revision: int = Field(ge=0)
    operation_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    transition_label: str = Field(min_length=1, max_length=200, pattern=IDENT)
    claim_id: str | None = Field(default=None, min_length=1, max_length=200, pattern=IDENT)
    fencing_token: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def claim_pair(self) -> StateRestoreRequest:
        if (self.claim_id is None) != (self.fencing_token is None):
            raise ValueError("claim_id and fencing_token must be supplied together")
        return self


class StateClaimRequest(StrictModel):
    contract_version: Literal["1.0"]
    object_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    scope_ref: str = Field(min_length=1, max_length=512)
    expected_revision: int = Field(ge=0)
    operation_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    ttl_seconds: int = Field(ge=1, le=3600)


class StateRenewRequest(StrictModel):
    contract_version: Literal["1.0"]
    claim_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    object_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    scope_ref: str = Field(min_length=1, max_length=512)
    fencing_token: int = Field(ge=1)
    operation_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    ttl_seconds: int = Field(ge=1, le=3600)


class StateReleaseRequest(StrictModel):
    contract_version: Literal["1.0"]
    claim_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    object_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    scope_ref: str = Field(min_length=1, max_length=512)
    fencing_token: int = Field(ge=1)
    operation_id: str = Field(min_length=1, max_length=200, pattern=IDENT)


class StateOperationInspectRequest(StrictModel):
    contract_version: Literal["1.0"]
    scope_ref: str = Field(min_length=1, max_length=512)
    operation_id: str = Field(min_length=1, max_length=200, pattern=IDENT)


class StateReceipt(StrictModel):
    contract_version: Literal["1.0"]
    receipt_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    action: Literal["state.create", "state.transition", "state.tombstone", "state.restore"]
    operation_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    request_digest: str = Field(pattern=HEX64)
    scope_ref: str = Field(min_length=1, max_length=512)
    principal_ref: str = Field(min_length=1, max_length=512)
    authorization_ref: str = Field(min_length=1, max_length=512)
    status: Literal["committed", "conflict", "refused"]
    state_ref: StateRef | None = None
    event_ref: str | None = Field(default=None, min_length=1, max_length=200, pattern=IDENT)
    issued_at: datetime
    reason_codes: tuple[str, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def committed_has_refs(self) -> StateReceipt:
        if self.status == "committed" and (self.state_ref is None or self.event_ref is None):
            raise ValueError("committed receipt requires state_ref and event_ref")
        return self


class StateClaimReceipt(StrictModel):
    contract_version: Literal["1.0"]
    receipt_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    action: Literal["state.claim", "state.renew", "state.release"]
    operation_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    request_digest: str = Field(pattern=HEX64)
    scope_ref: str = Field(min_length=1, max_length=512)
    object_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    principal_ref: str = Field(min_length=1, max_length=512)
    authorization_ref: str = Field(min_length=1, max_length=512)
    status: Literal["claimed", "renewed", "released", "refused", "conflict"]
    claim_id: str | None = Field(default=None, min_length=1, max_length=200, pattern=IDENT)
    fencing_token: int | None = Field(default=None, ge=1)
    holder_ref: str | None = Field(default=None, min_length=1, max_length=512)
    expires_at: datetime | None = None
    released_at: datetime | None = None
    issued_at: datetime
    reason_codes: tuple[str, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def claim_metadata_law(self) -> StateClaimReceipt:
        if self.status in {"claimed", "renewed"}:
            if (
                self.claim_id is None
                or self.fencing_token is None
                or self.holder_ref is None
                or self.expires_at is None
            ):
                raise ValueError(f"{self.status} receipt requires active claim metadata")
        if self.status == "released":
            if (
                self.claim_id is None
                or self.fencing_token is None
                or self.holder_ref is None
                or self.released_at is None
            ):
                raise ValueError("released receipt requires released claim metadata")
        return self


class StateReadResult(StrictModel):
    contract_version: Literal["1.0"]
    state_ref: StateRef
    payload_status: Literal["present", "erased"]
    payload: dict[str, Any] | None = Field(default=None, max_length=1000)
    recorded_at: datetime
    observed_at: datetime

    @model_validator(mode="after")
    def payload_law(self) -> StateReadResult:
        if self.payload_status == "present" and self.payload is None:
            raise ValueError("present payload_status requires payload")
        if self.payload_status == "erased" and self.payload is not None:
            raise ValueError("erased payload_status forbids payload")
        return self


class StateOperationInspectResult(StrictModel):
    contract_version: Literal["1.0"]
    scope_ref: str = Field(min_length=1, max_length=512)
    operation_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    status: Literal["found", "not_found"]
    request_digest: str | None = Field(default=None, pattern=HEX64)
    receipt_schema_ref: str | None = Field(default=None, min_length=1, max_length=512)
    receipt: dict[str, Any] | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def found_has_receipt(self) -> StateOperationInspectResult:
        if self.status == "found" and (
            self.request_digest is None or self.receipt_schema_ref is None or self.receipt is None
        ):
            raise ValueError("found inspect result requires digest, schema ref, and receipt")
        return self


class StateListRequest(StrictModel):
    contract_version: Literal["1.0"]
    scope_ref: str = Field(min_length=1, max_length=512)
    schema_refs: tuple[str, ...] = Field(min_length=1, max_length=20)
    lifecycle: Literal["active", "tombstoned", "erased"]
    cursor: str | None = Field(default=None, min_length=1, max_length=512)
    limit: int = Field(ge=1, le=200)
    consumer_ref: str = Field(min_length=1, max_length=512)


class StateEventReadRequest(StrictModel):
    contract_version: Literal["1.0"]
    scope_ref: str = Field(min_length=1, max_length=512)
    consumer_ref: str = Field(min_length=1, max_length=512)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)
    limit: int = Field(ge=1, le=200)


class StateAckRequest(StrictModel):
    contract_version: Literal["1.0"]
    scope_ref: str = Field(min_length=1, max_length=512)
    consumer_ref: str = Field(min_length=1, max_length=512)
    cursor: str = Field(min_length=1, max_length=512)
    operation_id: str = Field(min_length=1, max_length=200, pattern=IDENT)


class StateAckReceipt(StrictModel):
    contract_version: Literal["1.0"]
    receipt_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    operation_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    request_digest: str = Field(pattern=HEX64)
    scope_ref: str = Field(min_length=1, max_length=512)
    consumer_ref: str = Field(min_length=1, max_length=512)
    principal_ref: str = Field(min_length=1, max_length=512)
    authorization_ref: str = Field(min_length=1, max_length=512)
    status: Literal["committed", "conflict", "refused"]
    committed_cursor: str | None = Field(default=None, min_length=1, max_length=512)
    issued_at: datetime
    reason_codes: tuple[str, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def committed_has_cursor(self) -> StateAckReceipt:
        if self.status == "committed" and self.committed_cursor is None:
            raise ValueError("committed ack receipt requires committed_cursor")
        return self


class StateHistoryRequest(StrictModel):
    contract_version: Literal["1.0"]
    object_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    scope_ref: str = Field(min_length=1, max_length=512)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)
    limit: int = Field(ge=1, le=200)


class StateTransitionEvent(StrictModel):
    nullable_fields: ClassVar[frozenset[str]] = frozenset({"prior_revision"})

    contract_version: Literal["1.0"]
    event_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    journal_seq: int = Field(ge=1)
    event_kind: Literal["created", "transitioned", "tombstoned", "restored", "erased"]
    state_ref: StateRef
    prior_revision: int | None = Field(ge=0)
    transition_label: str | None = Field(default=None, min_length=1, max_length=200, pattern=IDENT)
    operation_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    principal_ref: str = Field(min_length=1, max_length=512)
    committed_at: datetime
    causation_ref: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def transition_label_law(self) -> StateTransitionEvent:
        if self.event_kind in {"transitioned", "restored"} and self.transition_label is None:
            raise ValueError(f"{self.event_kind} event requires transition_label")
        return self


class StateListPage(StrictModel):
    contract_version: Literal["1.0"]
    scope_ref: str = Field(min_length=1, max_length=512)
    consumer_ref: str = Field(min_length=1, max_length=512)
    states: tuple[StateRef, ...] = Field(max_length=200)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=512)
    coverage: Literal["complete", "bounded"]
    resume_event_cursor: str = Field(min_length=1, max_length=512)
    observed_at: datetime

    @model_validator(mode="after")
    def coverage_cursor_law(self) -> StateListPage:
        if self.coverage == "bounded" and self.next_cursor is None:
            raise ValueError("bounded list page requires next_cursor")
        if self.coverage == "complete" and self.next_cursor is not None:
            raise ValueError("complete list page forbids next_cursor")
        return self


class StateEventPage(StrictModel):
    contract_version: Literal["1.0"]
    scope_ref: str = Field(min_length=1, max_length=512)
    consumer_ref: str = Field(min_length=1, max_length=512)
    events: tuple[StateTransitionEvent, ...] = Field(max_length=200)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=512)
    coverage: Literal["complete", "bounded", "resync_required"]
    retained_from_seq: int | None = Field(default=None, ge=1)
    high_water_seq: int = Field(ge=0)
    observed_at: datetime

    @model_validator(mode="after")
    def event_cursor_law(self) -> StateEventPage:
        if self.coverage in {"complete", "bounded"} and self.next_cursor is None:
            raise ValueError("successful event page requires ackable next_cursor")
        if self.coverage == "resync_required":
            if self.events or self.next_cursor is not None or self.retained_from_seq is None:
                raise ValueError(
                    "resync_required page requires retained_from_seq and no events/cursor"
                )
        return self


class StateHistoryPage(StrictModel):
    contract_version: Literal["1.0"]
    object_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    scope_ref: str = Field(min_length=1, max_length=512)
    events: tuple[StateTransitionEvent, ...] = Field(max_length=200)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=512)
    coverage: Literal["complete", "bounded", "history_unavailable"]
    oldest_available_revision: int = Field(ge=0)
    latest_revision: int = Field(ge=0)
    observed_at: datetime

    @model_validator(mode="after")
    def history_cursor_law(self) -> StateHistoryPage:
        if self.coverage == "bounded" and self.next_cursor is None:
            raise ValueError("bounded history page requires next_cursor")
        if self.coverage in {"complete", "history_unavailable"} and self.next_cursor is not None:
            raise ValueError("complete/unavailable history page forbids next_cursor")
        return self


class StateProblem(StrictModel):
    contract_version: Literal["1.0"]
    status: Literal["failed"]
    code: str = Field(min_length=1, max_length=200, pattern=IDENT)
    retry_class: Literal[
        "no",
        "same_operation",
        "refresh_then_decide",
        "cold_resync",
        "later",
        "inspect_then_same_operation",
    ]
    operation_id: str | None = Field(default=None, min_length=1, max_length=200, pattern=IDENT)
    scope_ref: str | None = Field(default=None, min_length=1, max_length=512)
    current_state_ref: StateRef | None = None
    reason_codes: tuple[str, ...] = Field(max_length=32)
