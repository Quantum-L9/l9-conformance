from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic

from .errors import StateInfrastructureFailure

MonotonicClock = Callable[[], float]


@dataclass(frozen=True)
class ExecutionBudgetPolicy:
    """Private runtime policy for partitioning one trusted Gate request budget."""

    reconciliation_reserve_ms: int = 500
    return_reserve_ms: int = 100
    min_mutation_start_ms: int = 50
    reconciliation_poll_ms: int = 25

    def __post_init__(self) -> None:
        for name in (
            "reconciliation_reserve_ms",
            "return_reserve_ms",
            "min_mutation_start_ms",
            "reconciliation_poll_ms",
        ):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.reconciliation_poll_ms == 0:
            raise ValueError("reconciliation_poll_ms must be positive")


@dataclass(frozen=True)
class ExecutionBudget:
    """Provider-neutral execution budget derived from the trusted Gate packet budget.

    The monotonic clock is authority only for local budget consumption. Lease time remains
    storage-authoritative and is never derived from this object.
    """

    total_ms: int
    started_at: float
    policy: ExecutionBudgetPolicy = field(default_factory=ExecutionBudgetPolicy)
    _clock: MonotonicClock = field(default=monotonic, repr=False, compare=False)

    @classmethod
    def start(
        cls,
        timeout_ms: int,
        *,
        policy: ExecutionBudgetPolicy | None = None,
        clock: MonotonicClock = monotonic,
    ) -> ExecutionBudget:
        if timeout_ms < 1:
            raise ValueError("timeout_ms must be positive")
        resolved_policy = policy or ExecutionBudgetPolicy()
        return cls(
            total_ms=timeout_ms,
            started_at=clock(),
            policy=resolved_policy,
            _clock=clock,
        )

    @property
    def deadline(self) -> float:
        return self.started_at + (self.total_ms / 1000.0)

    def remaining_ms(self) -> int:
        remaining = int((self.deadline - self._clock()) * 1000)
        return max(0, remaining)

    def mutation_remaining_ms(self) -> int:
        return max(
            0,
            self.remaining_ms()
            - self.policy.reconciliation_reserve_ms
            - self.policy.return_reserve_ms,
        )

    def reconciliation_remaining_ms(self) -> int:
        return max(0, self.remaining_ms() - self.policy.return_reserve_ms)

    def can_start_mutation(self) -> bool:
        return self.mutation_remaining_ms() >= self.policy.min_mutation_start_ms

    def require_mutation_start(self, *, operation_id: str, scope_ref: str) -> None:
        if self.can_start_mutation():
            return
        raise StateInfrastructureFailure(
            code="DEADLINE_EXCEEDED",
            message="insufficient trusted request budget to begin State mutation",
            operation_id=operation_id,
            scope_ref=scope_ref,
        )
