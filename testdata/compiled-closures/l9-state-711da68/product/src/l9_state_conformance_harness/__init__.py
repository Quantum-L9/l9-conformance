"""State conformance harness: the product-owned code behind State's harness surface.

Each harness control and observation declared in ``contracts/STATE_HARNESS_SURFACE.yaml``
(``l9.harness-surface/v1``) names its realization here through ``implementation_ref``.

Boundary (verified by tests/test_conformance_harness_boundary.py):

* this package imports ``l9_state``; ``l9_state`` never imports this package;
* it is conformance-only and is excluded from every State wheel;
* nothing here is a Gate action, and nothing here grants one. ``hard_erase_current_revision``
  stays internal-only (STATE-RETENTION-002): it reaches StateStorePort directly, never Gate.
"""

from __future__ import annotations

from .controls import (
    FaultInjectingStore,
    FixtureClock,
    PolicyAuthorization,
    StoreFault,
    hard_erase_current_revision,
    trim_journal,
)
from .observations import gate_registration, operation_receipt_count

__all__ = [
    "FaultInjectingStore",
    "FixtureClock",
    "PolicyAuthorization",
    "StoreFault",
    "gate_registration",
    "hard_erase_current_revision",
    "operation_receipt_count",
    "trim_journal",
]
