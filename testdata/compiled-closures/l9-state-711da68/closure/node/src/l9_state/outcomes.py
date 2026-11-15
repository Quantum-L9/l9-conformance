from __future__ import annotations

from datetime import datetime
from typing import Literal

from .errors import ContractRefused, StateConflict, StateError
from .internal_models import HardEraseReceipt
from .models import StateClaimReceipt, StateReceipt

TerminalStatus = Literal["conflict", "refused"]


def terminal_status(exc: StateError) -> TerminalStatus:
    if isinstance(exc, ContractRefused):
        return "refused"
    if isinstance(exc, StateConflict):
        return "conflict"
    raise TypeError(f"unsupported logical terminal outcome: {type(exc)!r}")


def state_terminal_receipt(template: StateReceipt, exc: StateError) -> StateReceipt:
    return template.model_copy(
        update={
            "status": terminal_status(exc),
            "state_ref": None,
            "event_ref": None,
            "reason_codes": (exc.code,),
        }
    )


def claim_terminal_receipt(
    *,
    receipt_id: str,
    action: Literal["state.claim", "state.renew", "state.release"],
    operation_id: str,
    request_digest: str,
    scope_ref: str,
    object_id: str,
    principal_ref: str,
    authorization_ref: str,
    issued_at: datetime,
    exc: StateError,
) -> StateClaimReceipt:
    return StateClaimReceipt(
        contract_version="1.0",
        receipt_id=receipt_id,
        action=action,
        operation_id=operation_id,
        request_digest=request_digest,
        scope_ref=scope_ref,
        object_id=object_id,
        principal_ref=principal_ref,
        authorization_ref=authorization_ref,
        status=terminal_status(exc),
        issued_at=issued_at,
        reason_codes=(exc.code,),
    )


def hard_erase_terminal_receipt(template: HardEraseReceipt, exc: StateError) -> HardEraseReceipt:
    return template.model_copy(
        update={
            "status": terminal_status(exc),
            "state_ref": None,
            "event_ref": None,
            "reason_codes": (exc.code,),
        }
    )
