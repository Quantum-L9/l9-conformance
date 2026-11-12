"""Node artifact binding: verify the exact node subject before any case executes.

Two manifest formats are accepted:

``l9.semantic-compiler.node-artifact-manifest/v1`` (compiler-local schema, candidate)
    Bound from the compiler agent's text description (l9-semantic-compiler
    agent/r5/COMPILER @ 1d2b0a3, unpublished). The manifest lives at
    ``evidence/artifact-manifest.yaml`` inside the node tree; its digest is the
    *semantic* digest (canonical JSON of the parsed mapping) and must equal
    ``evidence/compilation-receipt.yaml`` ``output.digest``. The node tree must hold
    exactly: every ``generated[]`` file, every ``packaging.included_bound_sources[]``
    file, the declared ``pyproject.toml`` (if any), and the two evidence files.

``l9-conformance/synthetic-node-manifest@0`` (test-only)
    ``{files: [{path, sha256}]}`` held outside the tree; digest = raw file bytes.

Anything unverifiable raises :class:`ArtifactRefused`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

import yaml

from l9_conformance.digests import file_set_digest, sha256_canonical, sha256_file

COMPILER_MANIFEST_SCHEMA: Final = "l9.semantic-compiler.node-artifact-manifest/v1"
SYNTHETIC_MANIFEST_SCHEMA: Final = "l9-conformance/synthetic-node-manifest@0"
COMPILER_RECEIPT_SCHEMA: Final = "l9.compiler-receipt/v1"
COMPILER_RECEIPT_TYPE: Final = "l9.receipt/compilation@1"
COMPILER_IDENTITY_PREFIX: Final = "l9.receipt-instance/"
MANIFEST_REL: Final = "evidence/artifact-manifest.yaml"
COMPILATION_RECEIPT_REL: Final = "evidence/compilation-receipt.yaml"


class ArtifactRefused(RuntimeError):
    """The node artifact could not be verified."""


@dataclass(frozen=True)
class NodeArtifact:
    manifest_schema: str
    manifest_digest: str
    files: list[dict[str, str]]
    product_ref: str | None
    bound_source_digest: str | None
    compilation_receipt: dict[str, str] | None
    python_paths: tuple[str, ...]  # relative to the node root


def _contained(rel: str, where: str) -> str:
    p = Path(rel)
    if not rel or p.is_absolute() or ".." in p.parts:
        raise ArtifactRefused(f"{where}: path {rel!r} must be relative and contained")
    return p.as_posix()


def _entries(value: Any, where: str, digest_key: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ArtifactRefused(f"{where}: expected list")
    out: list[dict[str, str]] = []
    for i, raw in enumerate(cast(list[Any], value)):
        if not isinstance(raw, Mapping):
            raise ArtifactRefused(f"{where}[{i}]: expected mapping")
        entry = cast(Mapping[str, Any], raw)
        rel, digest = entry.get("path"), entry.get(digest_key)
        if not isinstance(rel, str) or not isinstance(digest, str):
            raise ArtifactRefused(f"{where}[{i}]: needs string path and {digest_key}")
        out.append({"path": _contained(rel, f"{where}[{i}]"), "sha256": digest})
    return out


def _tree_files(root: Path) -> set[str]:
    if not root.is_dir():
        raise ArtifactRefused(f"node artifact: directory {root} is missing")
    files: set[str] = set()
    for p in root.rglob("*"):
        if p.is_symlink():
            raise ArtifactRefused(f"node artifact: symlink {p.relative_to(root).as_posix()}")
        if p.is_file():
            files.add(p.relative_to(root).as_posix())
    return files


def _check_exact(root: Path, expected: Mapping[str, str | None]) -> list[dict[str, str]]:
    """Exact file set; ``None`` digest means bound by another mechanism (digest recorded)."""
    present = _tree_files(root)
    missing = sorted(set(expected) - present)
    extra = sorted(present - set(expected))
    if missing:
        raise ArtifactRefused(f"node artifact: bound files missing: {missing}")
    if extra:
        raise ArtifactRefused(f"node artifact: unbound files present: {extra}")
    out: list[dict[str, str]] = []
    for rel in sorted(expected):
        actual = sha256_file(root / rel)
        bound = expected[rel]
        if bound is not None and actual != bound:
            raise ArtifactRefused(
                f"node artifact: digest mismatch for {rel}: bound {bound}, actual {actual}"
            )
        out.append({"path": rel, "sha256": actual})
    return out


def _load_yaml_mapping(path: Path, label: str) -> Mapping[str, Any]:
    if not path.is_file():
        raise ArtifactRefused(f"{label}: {path} is missing")
    try:
        data = yaml.safe_load(path.read_bytes())
    except yaml.YAMLError as exc:
        raise ArtifactRefused(f"{label}: unparseable: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ArtifactRefused(f"{label}: expected mapping")
    return cast(Mapping[str, Any], data)


def verify_compiler_receipt(receipt: Mapping[str, Any], manifest_digest: str) -> dict[str, str]:
    """Self-verify a compiler receipt (l9.compiler-receipt/v1) and its binding to the manifest.

    receipt_identity = "l9.receipt-instance/" + hex(digest(receipt with
    receipt_identity="pending", without receipt_digest)); receipt_digest =
    digest(receipt without receipt_digest).
    """
    if receipt.get("schema") != COMPILER_RECEIPT_SCHEMA:
        raise ArtifactRefused("compilation receipt: schema is not l9.compiler-receipt/v1")
    if receipt.get("receipt_type") != COMPILER_RECEIPT_TYPE:
        raise ArtifactRefused("compilation receipt: receipt_type is not l9.receipt/compilation@1")
    if receipt.get("operation") != "target_artifacts":
        raise ArtifactRefused("compilation receipt: operation is not target_artifacts")
    recorded_digest = receipt.get("receipt_digest")
    recorded_identity = receipt.get("receipt_identity")
    if not isinstance(recorded_digest, str) or not isinstance(recorded_identity, str):
        raise ArtifactRefused("compilation receipt: receipt_identity/receipt_digest missing")
    without_digest = {k: v for k, v in receipt.items() if k != "receipt_digest"}
    if sha256_canonical(without_digest) != recorded_digest:
        raise ArtifactRefused("compilation receipt: receipt_digest does not verify")
    pending = {**without_digest, "receipt_identity": "pending"}
    expected_identity = COMPILER_IDENTITY_PREFIX + sha256_canonical(pending).removeprefix("sha256:")
    if recorded_identity != expected_identity:
        raise ArtifactRefused("compilation receipt: receipt_identity does not verify")
    output = receipt.get("output")
    if not isinstance(output, Mapping) or cast(Mapping[str, Any], output).get("digest") != (
        manifest_digest
    ):
        raise ArtifactRefused("compilation receipt: output.digest does not bind the manifest")
    return {"receipt_identity": recorded_identity, "receipt_digest": recorded_digest}


def _load_compiler(artifact_dir: Path, manifest_path: Path, expected: str) -> NodeArtifact:
    if manifest_path.resolve() != (artifact_dir / MANIFEST_REL).resolve():
        raise ArtifactRefused(f"compiler manifest must be the node tree's {MANIFEST_REL}")
    manifest = _load_yaml_mapping(manifest_path, "artifact manifest")
    digest = sha256_canonical(manifest)
    if digest != expected:
        raise ArtifactRefused(
            f"artifact manifest digest mismatch: bound {expected}, actual {digest}"
        )
    receipt = _load_yaml_mapping(artifact_dir / COMPILATION_RECEIPT_REL, "compilation receipt")
    receipt_binding = verify_compiler_receipt(receipt, digest)
    product_ref = manifest.get("product_ref")
    if not isinstance(product_ref, str) or not product_ref:
        raise ArtifactRefused("artifact manifest: product_ref missing")
    if receipt.get("exact_subject_ref") != product_ref:
        raise ArtifactRefused("compilation receipt: exact_subject_ref is not the manifest product")
    generated = _entries(manifest.get("generated"), "generated", "digest")
    if not generated:
        raise ArtifactRefused("artifact manifest: generated[] is empty")
    packaging = manifest.get("packaging")
    if not isinstance(packaging, Mapping):
        raise ArtifactRefused("artifact manifest: packaging missing")
    included = _entries(
        cast(Mapping[str, Any], packaging).get("included_bound_sources", []),
        "packaging.included_bound_sources",
        "digest",
    )
    expected_files: dict[str, str | None] = {}
    for entry in [*generated, *included]:
        if entry["path"] in expected_files and expected_files[entry["path"]] != entry["sha256"]:
            raise ArtifactRefused(f"artifact manifest: conflicting digests for {entry['path']}")
        expected_files[entry["path"]] = entry["sha256"]
    distribution = manifest.get("distribution")
    if isinstance(distribution, Mapping):
        dist = cast(Mapping[str, Any], distribution)
        if dist.get("status") == "declared":
            pyproject = dist.get("pyproject")
            if not isinstance(pyproject, Mapping):
                raise ArtifactRefused("artifact manifest: declared distribution lacks pyproject")
            pp = _entries([pyproject], "distribution.pyproject", "digest")[0]
            expected_files[pp["path"]] = pp["sha256"]
    # Evidence files are bound by the semantic manifest digest and the receipt's
    # self-digest above; their byte digests are recorded, not pre-bound.
    expected_files[MANIFEST_REL] = None
    expected_files[COMPILATION_RECEIPT_REL] = None
    files = _check_exact(artifact_dir, expected_files)
    python_paths = ["src", "."] if (artifact_dir / "src").is_dir() else ["."]
    return NodeArtifact(
        manifest_schema=COMPILER_MANIFEST_SCHEMA,
        manifest_digest=digest,
        files=files,
        product_ref=product_ref,
        bound_source_digest=file_set_digest(included),
        compilation_receipt=receipt_binding,
        python_paths=tuple(python_paths),
    )


def _load_synthetic(artifact_dir: Path, manifest_path: Path, expected: str) -> NodeArtifact:
    digest = sha256_file(manifest_path)
    if digest != expected:
        raise ArtifactRefused(
            f"artifact manifest digest mismatch: bound {expected}, actual {digest}"
        )
    manifest = _load_yaml_mapping(manifest_path, "artifact manifest")
    entries = _entries(manifest.get("files"), "artifact manifest files", "sha256")
    if not entries:
        raise ArtifactRefused("artifact manifest: files must be a non-empty list")
    files = _check_exact(artifact_dir, {e["path"]: e["sha256"] for e in entries})
    return NodeArtifact(
        manifest_schema=SYNTHETIC_MANIFEST_SCHEMA,
        manifest_digest=digest,
        files=files,
        product_ref=None,
        bound_source_digest=None,
        compilation_receipt=None,
        python_paths=(".",),
    )


def load(artifact_dir: Path, manifest_path: Path, expected_digest: str) -> NodeArtifact:
    manifest = _load_yaml_mapping(manifest_path, "artifact manifest")
    schema = manifest.get("schema")
    if schema == COMPILER_MANIFEST_SCHEMA:
        return _load_compiler(artifact_dir, manifest_path, expected_digest)
    if schema == SYNTHETIC_MANIFEST_SCHEMA:
        return _load_synthetic(artifact_dir, manifest_path, expected_digest)
    raise ArtifactRefused(f"artifact manifest schema {schema!r} is not supported")


def python_path(artifact: NodeArtifact, root: Path) -> str:
    """PYTHONPATH for a copy of the node tree at ``root``."""
    return os.pathsep.join(str((root / p).resolve()) for p in artifact.python_paths)
