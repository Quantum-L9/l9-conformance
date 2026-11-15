"""Harness controls: environment operations the conformance harness performs on State.

None of these is a Gate action. Each realizes one ``harness_controls`` entry of
``contracts/STATE_HARNESS_SURFACE.yaml``; the request each accepts is shaped by that entry's
``contract_ref`` (``contracts/STATE_HARNESS_CONTRACTS.yaml``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from l9_state.adapters.memory import InProcessStateStore
from l9_state.errors import StateInfrastructureFailure
from l9_state.execution import ExecutionBudget
from l9_state.internal_models import HardEraseReceipt
from l9_state.ports import ScopeAuthorizationDecision
from l9_state.retention import prepare_hard_erase_plan


class FixtureClock:
    """Clock control (kind ``clock``): the controllable clock State observes.

    Request ``{seconds}`` advances it; ``__call__`` is the ``now`` callable the store uses.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self.value = start if start is not None else datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("the fixture clock never moves backwards")
        self.value = self.value + timedelta(seconds=seconds)


class PolicyAuthorization:
    """AuthorizationPolicy control (kind ``authorization_policy``).

    A ScopeAuthorizationPort answering with the policy of request ``{deny?, delegated_consumers?}``:
    a request matching every field of a ``deny`` rule is denied (SCOPE_FORBIDDEN); a consumer
    other than the caller is denied unless delegated; everything else is allowed.
    """

    def __init__(self, spec: Mapping[str, Any]) -> None:
        self._deny = [dict(rule) for rule in spec.get("deny", ())]
        self._delegated = set(spec.get("delegated_consumers", ()))

    async def authorize(self, request: Any) -> ScopeAuthorizationDecision:
        for rule in self._deny:
            if all(getattr(request, key, None) == value for key, value in rule.items()):
                return ScopeAuthorizationDecision(
                    allowed=False, authorization_ref="policy.denied", reason_code="SCOPE_FORBIDDEN"
                )
        if request.consumer_ref not in (None, request.principal_ref) and (
            request.consumer_ref not in self._delegated
        ):
            return ScopeAuthorizationDecision(
                allowed=False, authorization_ref="policy.denied", reason_code="SCOPE_FORBIDDEN"
            )
        return ScopeAuthorizationDecision(
            allowed=True,
            authorization_ref=f"policy.scope:{request.principal_ref}:{request.action}",
        )


InfrastructureCode = Literal["STORAGE_UNAVAILABLE", "DEADLINE_EXCEEDED", "OUTCOME_UNKNOWN"]
INFRASTRUCTURE_CODES: frozenset[str] = frozenset(
    ("STORAGE_UNAVAILABLE", "DEADLINE_EXCEEDED", "OUTCOME_UNKNOWN")
)


@dataclass
class StoreFault:
    """One injected StateStorePort failure: ``{port_method, code, after_effect}``."""

    method: str
    code: InfrastructureCode
    after_effect: bool
    remaining: int = 1


@dataclass
class FaultInjectingStore:
    """StoreFault control (kind ``fault_injection``, target StateStorePort).

    Delegates every StateStorePort method to ``inner``. A pending fault for a method makes its
    next call fail with infrastructure ``code``: ``after_effect=False`` before the effect takes
    place, ``after_effect=True`` after the effect commits (an ambiguous commit).
    """

    inner: InProcessStateStore
    faults: list[StoreFault] = field(default_factory=list)

    def inject(self, request: Mapping[str, Any]) -> None:
        after = request["after_effect"]
        if after not in ("before", "after"):
            raise ValueError("after_effect must be 'before' or 'after'")
        code = str(request["code"])
        if code not in INFRASTRUCTURE_CODES:
            raise ValueError(f"undeclared infrastructure code {code!r}")
        self.faults.append(
            StoreFault(
                method=str(request["port_method"]),
                code=cast(InfrastructureCode, code),
                after_effect=after == "after",
            )
        )

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.inner, name)
        if not callable(target) or name.startswith("_"):
            return target

        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            fault = next((f for f in self.faults if f.method == name and f.remaining > 0), None)
            if fault is not None and not fault.after_effect:
                fault.remaining -= 1
                raise StateInfrastructureFailure(code=fault.code, message="injected fault")
            result = await target(*args, **kwargs)
            if fault is not None:
                fault.remaining -= 1
                raise StateInfrastructureFailure(
                    code=fault.code,
                    message="injected post-commit ambiguity",
                    operation_id=_operation_id_of(kwargs),
                )
            return result

        return wrapped


def _operation_id_of(kwargs: Mapping[str, Any]) -> str | None:
    operation = kwargs.get("operation")
    if operation is not None:
        return str(operation.receipt.operation_id)
    request = kwargs.get("request")
    operation_id = getattr(request, "operation_id", None)
    return None if operation_id is None else str(operation_id)


async def trim_journal(
    store: InProcessStateStore, *, tenant_org_id: str, scope_ref: str, journal_seq: int
) -> None:
    """JournalRetention control (kind ``journal_retention``): request ``{journal_seq}``.

    Moves the retention horizon so events with journal_seq below ``journal_seq`` are no longer
    retained.
    """
    if journal_seq < 1:
        raise ValueError("journal_seq must be positive")
    await store._trim_events_before_for_test(tenant_org_id, scope_ref, journal_seq)


async def hard_erase_current_revision(
    store: InProcessStateStore,
    *,
    tenant_org_id: str,
    scope_ref: str,
    object_id: str,
    operation_id: str,
) -> HardEraseReceipt:
    """HardErase control (kind ``internal_setup``, target StateStorePort).

    Hard-erases the current revision of ``object_id`` through StateStorePort.hard_erase with a
    plan from ``l9_state.retention.prepare_hard_erase_plan``, acting as the retention executor.
    Internal-only (STATE-RETENTION-002): never a Gate action.
    """
    current = await store.get_revision(tenant_org_id, scope_ref, object_id)
    if current is None:
        raise LookupError("hard erase setup requires an existing object")
    plan = prepare_hard_erase_plan(
        current=current,
        operation_id=operation_id,
        principal_ref="retention-executor",
        authorization_ref="retention-decision:fixture",
        budget=ExecutionBudget.start(30_000),
        claim_id=None,
        fencing_token=None,
    )
    return await store.hard_erase(
        revision=plan.revision,
        expected_revision=plan.expected_revision,
        expected_state_digest=plan.expected_state_digest,
        expected_retention_policy_ref=plan.expected_retention_policy_ref,
        expected_retention_policy_digest=plan.expected_retention_policy_digest,
        claim_id=plan.claim_id,
        fencing_token=plan.fencing_token,
        operation=plan.operation,
        event=plan.event,
        budget=plan.budget,
    )
