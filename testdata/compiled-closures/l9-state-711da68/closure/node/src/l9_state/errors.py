from __future__ import annotations

from typing import Literal


class StateError(Exception):
    code = "INVALID_REQUEST"


class StateNotFound(StateError):
    code = "OBJECT_NOT_FOUND"


class StateConflict(StateError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ContractRefused(StateError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class StateInfrastructureFailure(StateError):
    def __init__(
        self,
        *,
        code: Literal["STORAGE_UNAVAILABLE", "DEADLINE_EXCEEDED", "OUTCOME_UNKNOWN"],
        message: str,
        operation_id: str | None = None,
        scope_ref: str | None = None,
    ) -> None:
        self.code = code
        self.operation_id = operation_id
        self.scope_ref = scope_ref
        super().__init__(message)
