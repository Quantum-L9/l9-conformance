from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any, Literal

CursorPurpose = Literal["list", "events", "history"]
_MAX_CURSOR_CHARS = 512
_BINDING_DIGEST_BYTES = 16


class CursorError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class CursorClaims:
    purpose: CursorPurpose
    scope_binding: str
    position: int
    consumer_binding: str | None = None
    object_binding: str | None = None
    filter_digest: str | None = None
    resume_seq: int | None = None
    last_object_id: str | None = None


class CursorCodec:
    """Issue and verify provider-independent opaque State cursors.

    Cursor payloads contain compact binding digests rather than raw tenant, scope,
    consumer, or object identifiers. The cursor HMAC authenticates those bindings;
    the short digests keep every valid v1 cursor inside the public 512-character
    contract even when legal identifiers are near their maximum sizes.

    The HMAC key is runtime configuration and must be stable across process restarts
    for already-issued cursors to remain valid.
    """

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("cursor HMAC secret must be at least 32 bytes")
        self._secret = bytes(secret)

    @classmethod
    def ephemeral(cls) -> CursorCodec:
        """Create a process-local codec suitable only for conformance tests/staging."""
        return cls(secrets.token_bytes(32))

    @staticmethod
    def _b64encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _b64decode(value: str) -> bytes:
        padding = "=" * ((4 - len(value) % 4) % 4)
        try:
            return base64.urlsafe_b64decode((value + padding).encode("ascii"))
        except Exception as exc:  # pragma: no cover - defensive normalization
            raise CursorError("CURSOR_INVALID", "cursor encoding is invalid") from exc

    @staticmethod
    def _canonical(payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )

    @classmethod
    def _compact_digest(cls, raw: bytes) -> str:
        return cls._b64encode(hashlib.sha256(raw).digest()[:_BINDING_DIGEST_BYTES])

    @classmethod
    def scope_binding(cls, tenant_org_id: str, scope_ref: str) -> str:
        return cls._compact_digest(
            cls._canonical({"tenant_org_id": tenant_org_id, "scope_ref": scope_ref})
        )

    @classmethod
    def consumer_binding(cls, consumer_ref: str) -> str:
        return cls._compact_digest(cls._canonical({"consumer_ref": consumer_ref}))

    @classmethod
    def object_binding(cls, object_id: str) -> str:
        return cls._compact_digest(cls._canonical({"object_id": object_id}))

    def issue(self, claims: CursorClaims) -> str:
        payload: dict[str, Any] = {
            "v": 1,
            "p": claims.purpose,
            "b": claims.scope_binding,
            "n": claims.position,
        }
        if claims.consumer_binding is not None:
            payload["c"] = claims.consumer_binding
        if claims.object_binding is not None:
            payload["o"] = claims.object_binding
        if claims.filter_digest is not None:
            payload["f"] = claims.filter_digest
        if claims.resume_seq is not None:
            payload["r"] = claims.resume_seq
        if claims.last_object_id is not None:
            payload["l"] = claims.last_object_id
        raw = self._canonical(payload)
        mac = hmac.new(self._secret, raw, hashlib.sha256).digest()
        cursor = f"{self._b64encode(raw)}.{self._b64encode(mac)}"
        if len(cursor) > _MAX_CURSOR_CHARS:
            raise ValueError("issued cursor exceeds public 512-character contract")
        return cursor

    def decode(self, cursor: str, *, purpose: CursorPurpose) -> CursorClaims:
        if not cursor or len(cursor) > _MAX_CURSOR_CHARS or cursor.count(".") != 1:
            raise CursorError("CURSOR_INVALID", "cursor shape is invalid")
        payload_part, mac_part = cursor.split(".", 1)
        raw = self._b64decode(payload_part)
        supplied_mac = self._b64decode(mac_part)
        expected_mac = hmac.new(self._secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_mac, expected_mac):
            raise CursorError("CURSOR_INVALID", "cursor authentication failed")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise CursorError("CURSOR_INVALID", "cursor payload is invalid") from exc
        if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("p") != purpose:
            raise CursorError("CURSOR_INVALID", "cursor purpose or version is invalid")
        try:
            position = int(payload["n"])
            if position < 0:
                raise ValueError
            scope_binding = str(payload["b"])
            if not scope_binding:
                raise ValueError
            resume_seq = int(payload["r"]) if "r" in payload else None
            if resume_seq is not None and resume_seq < 0:
                raise ValueError
            return CursorClaims(
                purpose=purpose,
                scope_binding=scope_binding,
                position=position,
                consumer_binding=str(payload["c"]) if "c" in payload else None,
                object_binding=str(payload["o"]) if "o" in payload else None,
                filter_digest=str(payload["f"]) if "f" in payload else None,
                resume_seq=resume_seq,
                last_object_id=str(payload["l"]) if "l" in payload else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CursorError("CURSOR_INVALID", "cursor claims are invalid") from exc


def filter_digest(*, schema_refs: tuple[str, ...], lifecycle: str) -> str:
    payload = {
        "schema_refs": sorted(set(schema_refs)),
        "lifecycle": lifecycle,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return CursorCodec._compact_digest(raw)
