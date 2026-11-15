"""Harness observations: read-only views outside State's interface surface.

Each realizes one ``harness_observations`` entry of ``contracts/STATE_HARNESS_SURFACE.yaml``;
its result is shaped by that entry's ``result_contract_ref``.
"""

from __future__ import annotations

from typing import Any

from constellation_node_sdk.runtime.handlers import get_handler

from l9_state.adapters.memory import InProcessStateStore


def operation_receipt_count(store: InProcessStateStore) -> dict[str, Any]:
    """OperationReceiptCount observation: ``{count}`` durable operation receipts (request {})."""
    return {"count": len(store._operations)}


def gate_registration(action: str) -> dict[str, Any]:
    """GateRegistration observation: ``{registered}`` for request ``{action}``."""
    return {"registered": get_handler(action) is not None}
