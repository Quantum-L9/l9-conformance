"""Synthetic closure builder for runner tests.

Everything built here is SYNTHETIC and test-only: a stand-in conformance IR
(``l9-conformance/synthetic-conformance-ir@0``), a toy node artifact, and a toy
rendered suite. None of it encodes L9 law.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from l9_conformance.digests import sha256_file
from l9_conformance.runner import RunRequest

FAKE_REV = "0" * 40

SUITE_SOURCE = textwrap.dedent(
    """
    import pytest
    import toy_node


    def test_pass():
        assert toy_node.add(1, 2) == 3


    def test_fail():
        assert toy_node.add(1, 2) == 4


    @pytest.fixture
    def broken():
        raise RuntimeError("fixture setup failure")


    def test_error(broken):
        assert broken


    def test_skip():
        pytest.skip("not executed")


    @pytest.mark.parametrize("x", [1, 2])
    def test_param(x):
        assert x
    """
)


@dataclass
class Closure:
    root: Path
    ir_doc: dict[str, Any]

    def request(self, **overrides: Any) -> RunRequest:
        ir_path = self.root / "ir.yaml"
        ir_path.write_text(yaml.safe_dump(self.ir_doc, sort_keys=True), encoding="utf-8")
        manifest = self.root / "artifact-manifest.yaml"
        base: dict[str, Any] = {
            "ir_path": ir_path,
            "ir_digest": sha256_file(ir_path),
            "fixture_ir_path": self.root / "fixture-ir.yaml",
            "suite_dir": self.root / "suite",
            "artifact_dir": self.root / "artifact",
            "artifact_manifest_path": manifest,
            "artifact_manifest_digest": sha256_file(manifest),
            "source_revision": FAKE_REV,
            "bound_source_digest": "sha256:" + "1" * 64,
            "timeout_seconds": 60.0,
        }
        base.update(overrides)
        return RunRequest(**base)


ObligationSpec = dict[str, Any]


@pytest.fixture
def make_closure(tmp_path: Path) -> Callable[[list[ObligationSpec]], Closure]:
    def build(obligations: list[ObligationSpec]) -> Closure:
        artifact = tmp_path / "artifact"
        artifact.mkdir()
        (artifact / "toy_node.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        manifest = {
            "files": [{"path": "toy_node.py", "sha256": sha256_file(artifact / "toy_node.py")}]
        }
        (tmp_path / "artifact-manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        suite = tmp_path / "suite"
        suite.mkdir()
        (suite / "test_toy.py").write_text(SUITE_SOURCE, encoding="utf-8")
        fixture_ir = tmp_path / "fixture-ir.yaml"
        fixture_ir.write_text("schema: synthetic-fixture-ir@0\nsynthetic: true\n", encoding="utf-8")
        ir_doc: dict[str, Any] = {
            "schema": "l9-conformance/synthetic-conformance-ir@0",
            "synthetic": True,
            "ir_ref": "synthetic://toy-node/conformance-ir",
            "subject": {"product_ref": "synthetic://toy-node"},
            "fixture_ir": {
                "ref": "synthetic://toy-node/fixture-ir",
                "digest": sha256_file(fixture_ir),
            },
            "profiles": [],
            "canonical_authority": {
                "repository": "Quantum-L9/.github",
                "revision": FAKE_REV,
                "admitted": False,
            },
            "semantic_source_digests": [],
            "execution_plan": {"mode": "fixture_replay", "isolation": "fresh_subject_per_case"},
            "rendered_suite": {
                "files": [{"path": "test_toy.py", "sha256": sha256_file(suite / "test_toy.py")}]
            },
            "obligations": obligations,
        }
        return Closure(root=tmp_path, ir_doc=ir_doc)

    return build


def replay(oid: str, *tests: str, required: bool = True) -> ObligationSpec:
    return {
        "id": oid,
        "verification_class": "fixture_replay",
        "required": required,
        "cases": [{"case_id": f"{oid}/{t}", "nodeid": f"test_toy.py::{t}"} for t in tests],
    }
