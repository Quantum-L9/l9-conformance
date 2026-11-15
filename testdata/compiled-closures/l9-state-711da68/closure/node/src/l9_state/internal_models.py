from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .models import HEX64, IDENT, StateRef, StrictModel


class HardEraseReceipt(StrictModel):
    """Private durable receipt for retention-authorized hard erase.

    This is not a public Gate contract. It exists so internal erasure obeys the
    same immutable OperationKey/finality law as public State mutations.
    """

    contract_version: Literal["1.0"]
    receipt_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    action: Literal["state.internal.hard_erase"]
    operation_id: str = Field(min_length=1, max_length=200, pattern=IDENT)
    request_digest: str = Field(pattern=HEX64)
    scope_ref: str = Field(min_length=1, max_length=512)
    principal_ref: str = Field(min_length=1, max_length=512)
    authorization_ref: str = Field(min_length=1, max_length=512)
    status: Literal["committed", "conflict", "refused"]
    state_ref: StateRef | None = None
    event_ref: str | None = Field(default=None, min_length=1, max_length=200, pattern=IDENT)
    issued_at: datetime
    reason_codes: tuple[str, ...] = Field(default=(), max_length=32)
