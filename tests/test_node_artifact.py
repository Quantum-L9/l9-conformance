"""Binding to the compiler node-artifact manifest (node-artifact-manifest/v1).

The node trees built here are SYNTHETIC replicas shaped after the compiler agent's
text description (l9-semantic-compiler agent/r5/COMPILER @ 1d2b0a3). They are not
compiler output and encode no L9 law.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import Closure, ObligationSpec, replay

from l9_conformance.digests import file_set_digest, sha256_bytes, sha256_canonical
from l9_conformance.node_artifact import COMPILATION_RECEIPT_REL, MANIFEST_REL
from l9_conformance.receipt import verify_receipt
from l9_conformance.runner import RunnerRefusal, run

Build = Callable[[list[ObligationSpec]], Closure]
PRODUCT = "synthetic://toy-node"


def _dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=True, allow_unicode=True), encoding="utf-8")


def _seal_compiler_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    pending = {**receipt, "receipt_identity": "pending"}
    identity = "l9.receipt-instance/" + sha256_canonical(pending).removeprefix("sha256:")
    sealed = {**receipt, "receipt_identity": identity}
    sealed["receipt_digest"] = sha256_canonical(sealed)
    return sealed


def build_compiler_node(
    root: Path,
    *,
    product_ref: str = PRODUCT,
    declare_pyproject: bool = False,
    mutate_manifest: Callable[[dict[str, Any]], None] | None = None,
    mutate_receipt: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[Path, str]:
    """Write a node tree; return (manifest path, semantic manifest digest)."""
    gen = root / "src" / "toy_node" / "__init__.py"
    gen.parent.mkdir(parents=True)
    gen.write_text("from toy_core.ops import add as add\n", encoding="utf-8")
    bound = root / "src" / "toy_core" / "ops.py"
    bound.parent.mkdir(parents=True)
    bound.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "src" / "toy_core" / "__init__.py").write_text("", encoding="utf-8")
    manifest: dict[str, Any] = {
        "schema": "l9.semantic-compiler.node-artifact-manifest/v1",
        "product_ref": product_ref,
        "target_binding": "l9.binding/python-gate-node@1",
        "authority_revision": "Quantum-L9/.github@" + "0" * 40,
        "authority_status": "candidate_not_admitted",
        "package": "toy_node",
        "generated": [
            {
                "path": "src/toy_node/__init__.py",
                "artifact_ref": "l9.binding/python-gate-node@1#src/toy_node/__init__.py",
                "target_ir_digest": "sha256:" + "2" * 64,
                "digest": sha256_bytes(gen.read_bytes()),
            }
        ],
        "conformance": {"status": "schema_unbound"},
        "distribution": {"status": "not_declared"},
        "realized": False,
        "packaging": {
            "mode": "INCLUDE_VERIFIED_BOUND_SOURCE",
            "closure": "static_import",
            "included_bound_sources": [
                {"path": "src/toy_core/__init__.py", "digest": sha256_bytes(b"")},
                {"path": "src/toy_core/ops.py", "digest": sha256_bytes(bound.read_bytes())},
            ],
            "external_modules": [],
        },
    }
    if declare_pyproject:
        pyproject = root / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'toy'\n", encoding="utf-8")
        manifest["distribution"] = {
            "status": "declared",
            "pyproject": {"path": "pyproject.toml", "digest": sha256_bytes(pyproject.read_bytes())},
        }
    if mutate_manifest is not None:
        mutate_manifest(manifest)
    digest = sha256_canonical(manifest)
    receipt: dict[str, Any] = {
        "schema": "l9.compiler-receipt/v1",
        "receipt_type": "l9.receipt/compilation@1",
        "exact_subject_ref": product_ref,
        "operation": "target_artifacts",
        "output": {"ref": "l9.semantic-compiler.node-artifact-manifest/v1", "digest": digest},
        "result": "pass",
    }
    sealed = _seal_compiler_receipt(receipt)
    if mutate_receipt is not None:
        mutate_receipt(sealed)
    _dump(root / MANIFEST_REL, manifest)
    _dump(root / COMPILATION_RECEIPT_REL, sealed)
    return root / MANIFEST_REL, digest


SUITE = (
    "import toy_node\n\n"
    "def test_pass():\n    assert toy_node.add(2, 2) == 4\n\n"
    "def test_fail():\n    assert toy_node.add(2, 2) == 5\n"
)


@pytest.fixture
def compiler_closure(make_closure: Build, tmp_path: Path) -> Callable[..., tuple[Closure, Any]]:
    def build(**kwargs: Any) -> tuple[Closure, Any]:
        closure = make_closure([replay("ob/a", "test_pass")])
        suite = closure.root / "suite" / "test_toy.py"
        suite.write_text(SUITE, encoding="utf-8")
        closure.ir_doc["rendered_suite"]["files"][0]["sha256"] = sha256_bytes(suite.read_bytes())
        node = tmp_path / "node"
        manifest_path, digest = build_compiler_node(node, **kwargs)
        overrides = {
            "artifact_dir": node,
            "artifact_manifest_path": manifest_path,
            "artifact_manifest_digest": digest,
        }
        return closure, overrides

    return build


def test_compiler_node_binds_and_runs(compiler_closure: Callable[..., tuple[Closure, Any]]) -> None:
    closure, overrides = compiler_closure()
    receipt = run(closure.request(**overrides, bound_source_digest=None))
    verify_receipt(receipt)
    assert receipt["observation_or_outcome"]["overall_verdict"] == "pass"
    node = receipt["exact_subject_ref"]["node_artifact"]
    assert node["manifest_schema"] == "l9.semantic-compiler.node-artifact-manifest/v1"
    assert node["manifest_digest"] == overrides["artifact_manifest_digest"]
    assert node["compilation_receipt"]["receipt_identity"].startswith("l9.receipt-instance/")
    paths = {f["path"] for f in node["files"]}
    assert {MANIFEST_REL, COMPILATION_RECEIPT_REL, "src/toy_core/ops.py"} <= paths
    expected_bound = file_set_digest(
        [
            {"path": "src/toy_core/__init__.py", "sha256": sha256_bytes(b"")},
            {
                "path": "src/toy_core/ops.py",
                "sha256": sha256_bytes(b"def add(a, b):\n    return a + b\n"),
            },
        ]
    )
    assert receipt["exact_subject_ref"]["implementation"]["bound_source_digest"] == expected_bound


def test_compiler_node_fail_maps_to_fail(
    compiler_closure: Callable[..., tuple[Closure, Any]],
) -> None:
    closure, overrides = compiler_closure()
    closure.ir_doc["obligations"] = [replay("ob/a", "test_fail")]
    receipt = run(closure.request(**overrides, bound_source_digest=None))
    assert receipt["observation_or_outcome"]["overall_verdict"] == "fail"


def _set_generated_digest(m: dict[str, Any]) -> None:
    m["generated"][0]["digest"] = "sha256:" + "9" * 64


def _break_identity(r: dict[str, Any]) -> None:
    r["receipt_identity"] = "l9.receipt-instance/" + "0" * 64


def _break_operation(r: dict[str, Any]) -> None:
    r["operation"] = "implementation_ir"


def _unbind_output(r: dict[str, Any]) -> None:
    r["output"]["digest"] = "sha256:" + "8" * 64


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"mutate_manifest": _set_generated_digest}, "digest mismatch for src/toy_node"),
        ({"mutate_receipt": _break_identity}, "receipt_digest does not verify"),
        ({"mutate_receipt": _break_operation}, "operation is not target_artifacts"),
        ({"mutate_receipt": _unbind_output}, "receipt_digest does not verify"),
        ({"product_ref": "synthetic://other"}, "is not the IR subject"),
    ],
)
def test_compiler_node_refusals(
    compiler_closure: Callable[..., tuple[Closure, Any]], kwargs: dict[str, Any], match: str
) -> None:
    closure, overrides = compiler_closure(**kwargs)
    with pytest.raises(RunnerRefusal, match=match):
        run(closure.request(**overrides, bound_source_digest=None))


def test_resealed_receipt_not_binding_manifest_refused(tmp_path: Path) -> None:
    from l9_conformance.node_artifact import ArtifactRefused, verify_compiler_receipt

    receipt = _seal_compiler_receipt(
        {
            "schema": "l9.compiler-receipt/v1",
            "receipt_type": "l9.receipt/compilation@1",
            "operation": "target_artifacts",
            "output": {"digest": "sha256:" + "8" * 64},
        }
    )
    with pytest.raises(ArtifactRefused, match="does not bind the manifest"):
        verify_compiler_receipt(receipt, "sha256:" + "7" * 64)


def test_compiler_manifest_digest_mismatch_refused(
    compiler_closure: Callable[..., tuple[Closure, Any]],
) -> None:
    closure, overrides = compiler_closure()
    overrides["artifact_manifest_digest"] = "sha256:" + "6" * 64
    with pytest.raises(RunnerRefusal, match="artifact manifest digest mismatch"):
        run(closure.request(**overrides, bound_source_digest=None))


def test_generated_byte_change_refused(
    compiler_closure: Callable[..., tuple[Closure, Any]],
) -> None:
    closure, overrides = compiler_closure()
    (overrides["artifact_dir"] / "src" / "toy_node" / "__init__.py").write_text(
        "add = None\n", encoding="utf-8"
    )
    with pytest.raises(RunnerRefusal, match="digest mismatch"):
        run(closure.request(**overrides, bound_source_digest=None))


def test_unbound_file_in_node_refused(
    compiler_closure: Callable[..., tuple[Closure, Any]],
) -> None:
    closure, overrides = compiler_closure()
    (overrides["artifact_dir"] / "src" / "sitecustomize.py").write_text("", encoding="utf-8")
    with pytest.raises(RunnerRefusal, match="unbound files present"):
        run(closure.request(**overrides, bound_source_digest=None))


def test_supplied_bound_source_digest_must_match(
    compiler_closure: Callable[..., tuple[Closure, Any]],
) -> None:
    closure, overrides = compiler_closure()
    with pytest.raises(RunnerRefusal, match="bound source digest mismatch"):
        run(closure.request(**overrides, bound_source_digest="sha256:" + "5" * 64))


def test_manifest_outside_node_tree_refused(
    compiler_closure: Callable[..., tuple[Closure, Any]], tmp_path: Path
) -> None:
    closure, overrides = compiler_closure()
    outside = tmp_path / "outside-manifest.yaml"
    outside.write_bytes(overrides["artifact_manifest_path"].read_bytes())
    overrides["artifact_manifest_path"] = outside
    with pytest.raises(RunnerRefusal, match="must be the node tree"):
        run(closure.request(**overrides, bound_source_digest=None))


def test_declared_pyproject_bound(compiler_closure: Callable[..., tuple[Closure, Any]]) -> None:
    closure, overrides = compiler_closure(declare_pyproject=True)
    receipt = run(closure.request(**overrides, bound_source_digest=None))
    assert "pyproject.toml" in {
        f["path"] for f in receipt["exact_subject_ref"]["node_artifact"]["files"]
    }
    (overrides["artifact_dir"] / "pyproject.toml").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(RunnerRefusal, match=r"digest mismatch for pyproject\.toml"):
        run(closure.request(**overrides, bound_source_digest=None))


def test_unknown_manifest_schema_refused(make_closure: Build) -> None:
    closure = make_closure([replay("ob/a", "test_pass")])
    manifest = closure.root / "artifact-manifest.yaml"
    manifest.write_text("schema: something-else@1\nfiles: []\n", encoding="utf-8")
    with pytest.raises(RunnerRefusal, match="not supported"):
        run(closure.request())


def test_source_revision_recorded_as_caller_supplied(
    compiler_closure: Callable[..., tuple[Closure, Any]],
) -> None:
    closure, overrides = compiler_closure()
    receipt = run(closure.request(**overrides, bound_source_digest=None))
    implementation = receipt["exact_subject_ref"]["implementation"]
    assert implementation["source_revision_basis"] == "caller_supplied"


# --- l9-conformance/source-subject-manifest@0 ---------------------------------------------


def _source_subject(tmp_path: Path, **overrides: Any) -> tuple[Path, Path, str]:
    tree = tmp_path / "subject"
    (tree / "pkg").mkdir(parents=True)
    (tree / "pkg" / "__init__.py").write_bytes(b"")
    (tree / "pkg" / "mod.py").write_bytes(b"X = 1\n")
    from l9_conformance.digests import sha256_file

    manifest: dict[str, Any] = {
        "schema": "l9-conformance/source-subject-manifest@0",
        "source_revision": "Org/repo@" + "a" * 40,
        "source_path": "product/src",
        "packages": ["pkg"],
        "files": [
            {"path": p, "sha256": sha256_file(tree / p)} for p in ("pkg/__init__.py", "pkg/mod.py")
        ],
    }
    manifest.update(overrides)
    path = tmp_path / "subject-manifest.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    from l9_conformance.digests import sha256_file as _sf

    return tree, path, _sf(path)


def test_source_subject_binds_exact_tree(tmp_path: Path) -> None:
    from l9_conformance import node_artifact

    tree, path, digest = _source_subject(tmp_path)
    art = node_artifact.load(tree, path, digest)
    assert art.manifest_schema == "l9-conformance/source-subject-manifest@0"
    assert [f["path"] for f in art.files] == ["pkg/__init__.py", "pkg/mod.py"]
    assert art.compilation_receipt is None
    assert art.python_paths == (".",)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"source_revision": "Org/repo@main"}, "40-hex"),
        ({"source_path": "../escape"}, "contained"),
        ({"packages": []}, "non-empty"),
        ({"packages": ["not-ident"]}, "identifier"),
        ({"packages": ["other"]}, "outside the packages"),
    ],
)
def test_source_subject_refusals(tmp_path: Path, overrides: dict[str, Any], match: str) -> None:
    from l9_conformance import node_artifact

    tree, path, digest = _source_subject(tmp_path, **overrides)
    with pytest.raises(node_artifact.ArtifactRefused, match=match):
        node_artifact.load(tree, path, digest)


def test_source_subject_unbound_and_changed_files_refused(tmp_path: Path) -> None:
    from l9_conformance import node_artifact

    tree, path, digest = _source_subject(tmp_path)
    (tree / "pkg" / "extra.py").write_bytes(b"")
    with pytest.raises(node_artifact.ArtifactRefused, match="unbound"):
        node_artifact.load(tree, path, digest)
    (tree / "pkg" / "extra.py").unlink()
    (tree / "pkg" / "mod.py").write_bytes(b"X = 2\n")
    with pytest.raises(node_artifact.ArtifactRefused, match="digest mismatch"):
        node_artifact.load(tree, path, digest)
    with pytest.raises(node_artifact.ArtifactRefused, match="digest mismatch"):
        node_artifact.load(tree, path, "sha256:" + "0" * 64)
