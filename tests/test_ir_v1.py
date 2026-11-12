"""l9.conformance-ir/v1 adapter. IR, fixture IR and suite binding here are SYNTHETIC,
shaped on Quantum-L9/.github@44e02a09 semantics/conformance_ir.schema.yaml (F1, not
admitted); the suite-binding format is this repository's interim input (pending the
compiler's pytest/v2 rendering)."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import Closure, ObligationSpec

from l9_conformance.digests import sha256_canonical, sha256_file
from l9_conformance.receipt import verify_receipt
from l9_conformance.runner import RunnerRefusal, run

Build = Callable[[list[ObligationSpec]], Closure]
REV = "4" * 40
RULE = "pass_only_when_every_witness_case_executes_and_every_assertion_holds"


def _obligation(oid: str, cases: list[str], disposition: str = "executable") -> dict[str, Any]:
    o: dict[str, Any] = {
        "id": oid,
        "subject_class": "product_invariant",
        "governing_refs": ["synthetic://law/" + oid],
        "observable_semantics": "synthetic",
        "verification_class": "fixture_replay",
        "witnessed_by": cases,
        "disposition": disposition,
    }
    if disposition == "executable":
        o["execution_plan"] = {
            "mode": "fixture_replay",
            "cases": list(cases),
            "isolation": "fresh_subject_per_case",
            "verdict_rule": RULE,
        }
    return o


def _v1_doc(fixture_semantic: str) -> dict[str, Any]:
    return {
        "schema": "l9.conformance-ir/v1",
        "subject": {
            "product_ref": "synthetic://toy-node",
            "closure_digest": "sha256:" + "c" * 64,
            "fixture_ir_digest": fixture_semantic,
        },
        "authority": {
            "authority_revision": f"Quantum-L9/.github@{REV}",
            "compilation_profile_ref": "l9.compilation/product-build@1",
            "stage": "conformance",
        },
        "fixture_profile": {"status": "absent", "profile_refs": []},
        "obligations": [
            _obligation("ob/pass", ["case.pass"]),
            _obligation("ob/held", ["case.held"], disposition="unresolved"),
        ],
        "unresolved": [
            {"obligation_ref": "ob/held", "result": "unresolved", "reason": "no harness control"}
        ],
        "unknowns": [],
        "provenance": {},
    }


class V1:
    def __init__(self, closure: Closure) -> None:
        self.closure = closure
        root = closure.root
        fixture = root / "fixture-ir.yaml"
        fixture.write_text("schema: synthetic-fixture-ir@0\nlaws: []\n", encoding="utf-8")
        self.doc = _v1_doc(sha256_canonical(yaml.safe_load(fixture.read_bytes())))
        self.binding: dict[str, Any] = {
            "schema": "l9-conformance/suite-binding@0",
            "ir_ref": "synthetic://toy-node/conformance-ir",
            "fixture_ir_ref": "synthetic://toy-node/fixture-ir",
            "canonical_admitted": False,
            "files": [
                {"path": "test_toy.py", "sha256": sha256_file(root / "suite" / "test_toy.py")}
            ],
            "cases": {"case.pass": "test_toy.py::test_pass", "case.held": "test_toy.py::test_fail"},
        }

    def request(self, **overrides: Any):
        root = self.closure.root
        ir_path = root / "ir-v1.json"
        ir_path.write_text(yaml.safe_dump(self.doc, sort_keys=True), encoding="utf-8")
        binding = root / "suite-binding.yaml"
        binding.write_text(yaml.safe_dump(self.binding, sort_keys=True), encoding="utf-8")
        base: dict[str, Any] = {
            "ir_path": ir_path,
            "ir_digest": sha256_file(ir_path),
            "suite_binding_path": binding,
            "suite_binding_digest": sha256_file(binding),
            "ir_semantic_digest": sha256_canonical(self.doc),
        }
        base.update(overrides)
        return self.closure.request(**base)


@pytest.fixture
def v1(make_closure: Build) -> V1:
    return V1(make_closure([]))


def test_v1_run_executes_executable_and_holds_unresolved(v1: V1) -> None:
    receipt = run(v1.request())
    verify_receipt(receipt)
    obligations = {o["obligation_id"]: o for o in receipt["observation_or_outcome"]["obligations"]}
    assert obligations["ob/pass"]["verdict"] == "pass"
    held = obligations["ob/held"]
    # Mapped to a failing test, but never executed: the IR says unresolved.
    assert held["verdict"] == "unresolved" and held["cases"] == []
    assert "no harness control" in held["reason"]
    assert receipt["observation_or_outcome"]["overall_verdict"] == "unresolved"
    assert receipt["evidence_grade"] == "candidate_authority_rehearsal"
    authority = receipt["governing_coordinates"]["canonical_authority"]
    assert authority == {"repository": "Quantum-L9/.github", "revision": REV, "admitted": False}
    fixture_path = v1.closure.root / "fixture-ir.yaml"
    assert receipt["governing_coordinates"]["fixture_ir"]["digest"] == sha256_file(fixture_path)


def test_v1_declared_profile_binds_catalog_file_digest(v1: V1) -> None:
    v1.doc["fixture_profile"] = {
        "status": "declared",
        "profile_refs": ["l9.fixture-profile/core@1"],
    }
    receipt = run(v1.request())
    root = Path(__file__).resolve().parents[1]
    digest = sha256_file(root / "catalog" / "fixture-profile-core-1.yaml")
    assert receipt["governing_coordinates"]["profiles"] == [
        {"ref": "l9.fixture-profile/core@1", "digest": digest}
    ]


def _drop_plan_case(d: dict[str, Any]) -> None:
    d["obligations"][0]["execution_plan"]["cases"] = []


def _exec_listed_unresolved(d: dict[str, Any]) -> None:
    d["unresolved"].append({"obligation_ref": "ob/pass", "result": "unresolved", "reason": "x"})


def _unresolved_not_listed(d: dict[str, Any]) -> None:
    d["unresolved"] = []


def _unknown_unresolved_ref(d: dict[str, Any]) -> None:
    d["unresolved"].append({"obligation_ref": "ob/ghost", "result": "unsat", "reason": "x"})


def _bad_profile_ref(d: dict[str, Any]) -> None:
    d["fixture_profile"] = {"status": "declared", "profile_refs": []}


def _wrong_stage(d: dict[str, Any]) -> None:
    d["authority"]["stage"] = "target_artifacts"


def _bad_revision(d: dict[str, Any]) -> None:
    d["authority"]["authority_revision"] = "Quantum-L9/.github@main"


def _exec_non_replay(d: dict[str, Any]) -> None:
    d["obligations"][0]["verification_class"] = "static_inspection"


IR_REFUSALS: list[tuple[Callable[[dict[str, Any]], None], str]] = [
    (_drop_plan_case, "differ from witnessed_by"),
    (_exec_listed_unresolved, "executable obligation listed as unresolved"),
    (_unresolved_not_listed, "not listed in ir.unresolved"),
    (_unknown_unresolved_ref, "unknown obligations"),
    (_bad_profile_ref, "declared without profile_refs"),
    (_wrong_stage, "stage"),
    (_bad_revision, "authority_revision"),
    (_exec_non_replay, "executable requires fixture_replay"),
]


@pytest.mark.parametrize(("mutate", "match"), IR_REFUSALS)
def test_v1_ir_refusals(v1: V1, mutate: Callable[[dict[str, Any]], None], match: str) -> None:
    mutate(v1.doc)
    with pytest.raises(RunnerRefusal, match=match):
        run(v1.request())


def test_v1_semantic_digest_mismatch_refused(v1: V1) -> None:
    with pytest.raises(RunnerRefusal, match="semantic digest mismatch"):
        run(v1.request(ir_semantic_digest="sha256:" + "0" * 64))


def test_v1_fixture_semantic_digest_mismatch_refused(v1: V1) -> None:
    (v1.closure.root / "fixture-ir.yaml").write_text("schema: other\n", encoding="utf-8")
    with pytest.raises(RunnerRefusal, match="fixture IR digest mismatch"):
        run(v1.request())


def test_v1_requires_suite_binding(v1: V1) -> None:
    with pytest.raises(RunnerRefusal, match="requires a digest-bound suite binding"):
        run(v1.request(suite_binding_path=None))


def test_v1_suite_binding_digest_mismatch_refused(v1: V1) -> None:
    with pytest.raises(RunnerRefusal, match="suite binding digest mismatch"):
        run(v1.request(suite_binding_digest="sha256:" + "1" * 64))


def test_v1_case_without_rendered_test_refused(v1: V1) -> None:
    del v1.binding["cases"]["case.pass"]
    with pytest.raises(RunnerRefusal, match="has no rendered test"):
        run(v1.request())


def test_v1_raw_ir_bytes_still_bound(v1: V1) -> None:
    request = v1.request()
    doc = copy.deepcopy(v1.doc)
    (v1.closure.root / "ir-v1.json").write_text(yaml.safe_dump(doc, indent=4), encoding="utf-8")
    with pytest.raises(RunnerRefusal, match="conformance IR digest mismatch"):
        run(request)
