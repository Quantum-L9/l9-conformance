"""Deterministic digests used for every binding the runner checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

PREFIX = "sha256:"


def sha256_bytes(data: bytes) -> str:
    return PREFIX + hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_json(value: Any) -> bytes:
    """Canonical JSON: sorted keys, compact separators, UTF-8, no NaN."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_canonical(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def file_set_digest(files: list[dict[str, str]]) -> str:
    """Digest of a {path, sha256} set, independent of listing order."""
    ordered = sorted(
        ({"path": f["path"], "sha256": f["sha256"]} for f in files), key=lambda f: f["path"]
    )
    return sha256_canonical(ordered)
