"""Conformance-harness binding (coordinator ruling D5).

A product may keep conformance-only harness code under a declared harness root
(compiler ADR-0002: never packaged into the node). The runner executes it, so the
receipt binds it: every file under the root is hashed before any case runs, and the
driver reference must name a module inside that root.

Binding all files under the root (not only the driver module) covers every module
the run can import from the root. Bytecode and symlinks are refused because they
could execute or redirect to bytes that are not the bound source.

A harness may also read product data files at fixed product-relative paths (State's
driver reads its topology and harness contracts from ``Path(__file__).parents[2]``).
Those are declared as ``data_files``, bound by raw sha256 like the root's files, and the
runner lays the harness out as a product tree: the root at its declared path and each
data file at its own, so the harness sees exactly the bound bytes and nothing else.
"""

from __future__ import annotations

import re
import shutil
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
    product_dir: Path | None = None
    data_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class HarnessBinding:
    root: str
    source_revision: str
    driver_ref: str
    package: str
    files: list[dict[str, str]]
    digest: str
    data_files: tuple[str, ...] = ()


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
    files.extend(_bind_data_files(request, root))
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
        data_files=tuple(sorted(request.data_files)),
    )


def _bind_data_files(request: HarnessRequest, root: PurePosixPath) -> list[dict[str, str]]:
    if not request.data_files:
        return []
    product = request.product_dir
    if product is None:
        raise HarnessRefused("harness data_files require the product directory")
    if request.root_dir.resolve() != (product / root.as_posix()).resolve():
        raise HarnessRefused("harness root directory is not the declared root of the product")
    out: list[dict[str, str]] = []
    for rel in sorted(set(request.data_files)):
        p = PurePosixPath(rel)
        if not rel or p.is_absolute() or ".." in p.parts:
            raise HarnessRefused(f"harness data file {rel!r} must be relative and contained")
        if p == root or root in p.parents:
            raise HarnessRefused(f"harness data file {rel!r} is inside the harness root")
        path = product / rel
        if path.is_symlink() or not path.is_file():
            raise HarnessRefused(f"harness data file {rel!r} is missing or not a regular file")
        out.append({"path": p.as_posix(), "sha256": sha256_file(path)})
    return out


def lay_out(binding: HarnessBinding, request: HarnessRequest, product_copy: Path) -> Path:
    """Copy the bound harness into ``product_copy`` as a product tree; return its import root."""
    root_copy = product_copy / binding.root
    root_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(request.root_dir, root_copy)
    if binding.data_files:
        if request.product_dir is None:
            raise HarnessRefused("harness data_files require the product directory")
        for rel in binding.data_files:
            target = product_copy / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(request.product_dir / rel, target)
    return root_copy.parent


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


def copy_digest(binding: HarnessBinding, product_copy: Path) -> str:
    """Re-hash a laid-out harness product tree (TOCTOU check); every file counts."""
    files = [
        {"path": p.relative_to(product_copy).as_posix(), "sha256": sha256_file(p)}
        for p in sorted(product_copy.rglob("*"))
        if p.is_file() or p.is_symlink()
    ]
    return file_set_digest(files)
