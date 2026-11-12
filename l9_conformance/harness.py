"""Conformance-harness binding (coordinator ruling D5).

A product may keep conformance-only harness code under a declared harness root
(compiler ADR-0002: never packaged into the node). The runner executes it, so the
receipt binds it: every file under the root is hashed before any case runs, and the
driver reference must name a module inside that root.

Binding all files under the root (not only the driver module) covers every module
the run can import from the root. Bytecode and symlinks are refused because they
could execute or redirect to bytes that are not the bound source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from l9_conformance.digests import file_set_digest, sha256_file

_DRIVER_REF: Final = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$"
)


class HarnessRefused(RuntimeError):
    """The harness could not be bound."""


@dataclass(frozen=True)
class HarnessRequest:
    """Caller-supplied harness location and coordinates.

    ``root_dir`` is the local directory holding the harness root (a Python package
    directory); ``root`` is its declared repository-relative path; ``driver_ref`` is
    ``package.module:callable`` whose top-level package is the root's directory name.
    """

    root_dir: Path
    root: str
    source_revision: str
    driver_ref: str


@dataclass(frozen=True)
class HarnessBinding:
    root: str
    source_revision: str
    driver_ref: str
    package: str
    files: list[dict[str, str]]
    digest: str


def bind(request: HarnessRequest) -> HarnessBinding:
    root = PurePosixPath(request.root)
    if not request.root or root.is_absolute() or ".." in root.parts:
        raise HarnessRefused(f"harness root {request.root!r} must be relative and contained")
    if not request.source_revision:
        raise HarnessRefused("harness source revision is required")
    if not _DRIVER_REF.fullmatch(request.driver_ref):
        raise HarnessRefused(f"harness driver_ref {request.driver_ref!r} is not module:callable")
    root_dir = request.root_dir
    if not root_dir.is_dir() or root_dir.is_symlink():
        raise HarnessRefused(f"harness root directory {root_dir} is missing or a symlink")
    package = root_dir.name
    if package != root.name:
        raise HarnessRefused(
            f"harness root directory {package!r} does not match declared root {request.root!r}"
        )
    files: list[dict[str, str]] = []
    for path in sorted(root_dir.rglob("*")):
        rel = path.relative_to(root_dir).as_posix()
        if path.is_symlink():
            raise HarnessRefused(f"harness file {rel} is a symlink")
        if path.is_dir():
            if path.name == "__pycache__":
                raise HarnessRefused(f"harness contains bytecode directory {rel}")
            continue
        if path.suffix in {".pyc", ".pyo"}:
            raise HarnessRefused(f"harness contains bytecode file {rel}")
        files.append({"path": f"{root.as_posix()}/{rel}", "sha256": sha256_file(path)})
    if not files:
        raise HarnessRefused(f"harness root {request.root!r} holds no files")
    module, _callable = request.driver_ref.split(":", 1)
    parts = module.split(".")
    if parts[0] != package:
        raise HarnessRefused(
            f"harness driver module {module!r} is not inside harness root package {package!r}"
        )
    rel_parts = parts[1:]
    base = f"{root.as_posix()}/{'/'.join(rel_parts)}" if rel_parts else root.as_posix()
    candidates = [f"{base}.py", f"{base}/__init__.py"] if rel_parts else [f"{base}/__init__.py"]
    bound_paths = {f["path"] for f in files}
    if not any(c in bound_paths for c in candidates):
        raise HarnessRefused(f"harness driver module {module!r} has no bound source file")
    return HarnessBinding(
        root=root.as_posix(),
        source_revision=request.source_revision,
        driver_ref=request.driver_ref,
        package=package,
        files=files,
        digest=file_set_digest(files),
    )


def coordinates(binding: HarnessBinding) -> dict[str, object]:
    """The receipt's ``governing_coordinates.harness`` block."""
    return {
        "source_revision": binding.source_revision,
        "source_revision_basis": "caller_supplied",
        "root": binding.root,
        "driver_ref": binding.driver_ref,
        "digest": binding.digest,
        "files": binding.files,
    }


def copy_digest(binding: HarnessBinding, copy_dir: Path) -> str:
    """Re-hash a copied harness tree with the binding's path scheme (TOCTOU check)."""
    files = [
        {
            "path": f"{binding.root}/{p.relative_to(copy_dir).as_posix()}",
            "sha256": sha256_file(p),
        }
        for p in sorted(copy_dir.rglob("*"))
        if p.is_file()
    ]
    return file_set_digest(files)
