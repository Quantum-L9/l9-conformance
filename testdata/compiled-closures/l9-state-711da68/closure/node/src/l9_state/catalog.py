from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .errors import ContractRefused


class ConfiguredContractCatalog:
    """In-process conformance catalog; schema dialect remains adapter-private."""

    def __init__(self) -> None:
        self._schemas: dict[tuple[str, str], Callable[[dict[str, Any]], bool]] = {}
        self._retention: set[tuple[str, str]] = set()

    def admit_schema(
        self, ref: str, digest: str, validator: Callable[[dict[str, Any]], bool]
    ) -> None:
        self._schemas[(ref, digest)] = validator

    def admit_retention(self, ref: str, digest: str) -> None:
        self._retention.add((ref, digest))

    async def validate_state_payload(
        self, schema_ref: str, schema_digest: str, payload: dict[str, Any]
    ) -> str:
        validator = self._schemas.get((schema_ref, schema_digest))
        if validator is None:
            if any(ref == schema_ref for ref, _ in self._schemas):
                raise ContractRefused(
                    "SCHEMA_DIGEST_MISMATCH", "schema digest is not admitted for ref"
                )
            raise ContractRefused("SCHEMA_NOT_ADMITTED", "schema ref+digest is not admitted")
        if not validator(payload):
            raise ContractRefused("INVALID_REQUEST", "payload rejected by admitted schema")
        return f"schema:{schema_ref}:{schema_digest}"

    async def assert_retention_contract(
        self, retention_policy_ref: str, retention_policy_digest: str
    ) -> str:
        if (retention_policy_ref, retention_policy_digest) not in self._retention:
            raise ContractRefused(
                "RETENTION_POLICY_NOT_ADMITTED", "retention contract is not admitted"
            )
        return f"retention:{retention_policy_ref}:{retention_policy_digest}"
