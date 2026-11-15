from __future__ import annotations

import hashlib
from typing import Any

import rfc8785


def canonical_bytes(value: Any) -> bytes:
    return rfc8785.dumps(value)


def sha256_jcs(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def state_digest(
    *, schema_ref: str, schema_digest: str, lifecycle: str, payload: dict[str, Any] | None
) -> str:
    return sha256_jcs(
        {
            "schema_ref": schema_ref,
            "schema_digest": schema_digest,
            "lifecycle": lifecycle,
            "payload": payload,
        }
    )


def request_digest(action: str, domain_request: dict[str, Any]) -> str:
    semantic = dict(domain_request)
    semantic.pop("operation_id", None)
    return sha256_jcs({"action": action, "domain_request": semantic})
