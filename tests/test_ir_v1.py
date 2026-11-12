"""l9.conformance-ir/v1 adapter + pytest/v2 rendered suite.

Everything here is SYNTHETIC: the IR is shaped on Quantum-L9/.github@44e02a09
semantics/conformance_ir.schema.yaml (F1, not admitted) and the four-file suite imitates
the compiler's pytest/v2 layout as described by COMPILER (conftest outcome record keyed
by the exact case id via ``l9_case``; tests named ``_identifier(case_id)``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import Closure, ObligationSpec

from l9_conformance.digests import sha256_canonical, sha256_file
from l9_conformance.harness import HarnessRequest
from l9_conformance.ir_v1 import identifier
from l9_conformance.receipt import ReceiptInvalid, verify_receipt, verify_receipt_inputs
from l9_conformance.runner import RunnerRefusal, RunRequest, run

Build = Callable[[list[ObligationSpec]], Closure]
REV = "4" * 40
RULE = "pass_only_when_every_witness_case_executes_and_every_assertion_holds"
CLOSURE = "sha256:" + "c" * 64

CONFTEST = """
import json, os
import pytest

_OUT = {}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    case = getattr(getattr(item, "function", None), "l9_case", None)
    if case is None:
        return
    if rep.when == "setup" and rep.failed:
        _OUT[case] = "error"
    elif rep.when == "call":
        if rep.passed:
            _OUT[case] = "pass"
        elif call.excinfo is not None and call.excinfo.errisinstance(AssertionError):
            _OUT[case] = "fail"
        else:
            _OUT[case] = "error"


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("L9_CONFORMANCE_OUTCOMES")
    if path:
        with open(path, "w") as f:
            f.write(json.dumps(_OUT, sort_keys=True, indent=1))
"""

TESTS = """
import os


def _case(case_id):
    def deco(fn):
        fn.l9_case = case_id
        return fn
    return deco


@_case("case.pass")
def test_case_pass():
    assert True


@_case("case:odd/id")
def test_case_odd_id():
    assert True


@_case("case.fail")
def test_case_fail():
    assert False


def test_case_untagged():
    assert True


@_case("case.driver")
def test_case_driver():
    assert os.environ["L9_CONFORMANCE_DRIVER"] == "toy_harness.driver:create_driver"
"""


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


class V1:
    def __init__(self, closure: Closure) -> None:
        self.closure = closure
        root = closure.root
        suite = root / "suite"
        (suite / "test_toy.py").unlink()
        self.write_suite(CONFTEST)
        (suite / "l9_conformance_eval.py").write_text("# evaluators\n", encoding="utf-8")
        (suite / "l9_conformance_cases.json").write_text(
            json.dumps({"binding": {}, "cases": [], "closure_digest": CLOSURE}, sort_keys=True),
            encoding="utf-8",
        )
        fixture = root / "fixture-ir.yaml"
        fixture.write_text("schema: synthetic-fixture-ir@0\nlaws: []\n", encoding="utf-8")
        self.doc: dict[str, Any] = {
            "schema": "l9.conformance-ir/v1",
            "subject": {
                "product_ref": "synthetic://toy-node",
                "closure_digest": CLOSURE,
                "fixture_ir_digest": sha256_canonical(yaml.safe_load(fixture.read_bytes())),
            },
            "authority": {
                "authority_revision": f"Quantum-L9/.github@{REV}",
                "compilation_profile_ref": "l9.compilation/product-build@1",
                "stage": "conformance",
            },
            "fixture_profile": {"status": "absent", "profile_refs": []},
            "obligations": [
                _obligation("ob/pass", ["case.pass", "case:odd/id"]),
                _obligation("ob/fail", ["case.fail"]),
                _obligation("ob/held", ["case.held"], disposition="unresolved"),
            ],
            "unresolved": [
                {"obligation_ref": "ob/held", "result": "unresolved", "reason": "no control"}
            ],
            "unknowns": [],
            "provenance": {},
        }
        self.binding: dict[str, Any] = {
            "schema": "l9-conformance/suite-binding@1",
            "layout": "pytest/v2",
            "ir_ref": "synthetic://toy-node/conformance-ir",
            "fixture_ir_ref": "synthetic://toy-node/fixture-ir",
            "canonical_admitted": False,
        }

    def write_suite(self, conftest: str, tests: str = TESTS) -> None:
        suite = self.closure.root / "suite"
        (suite / "conftest.py").write_text(conftest, encoding="utf-8")
        (suite / "test_l9_conformance.py").write_text(tests, encoding="utf-8")

    def request(self, **overrides: Any) -> RunRequest:
        root = self.closure.root
        suite = root / "suite"
        binding = dict(self.binding)
        binding.setdefault(
            "files",
            [{"path": p.name, "sha256": sha256_file(p)} for p in sorted(suite.iterdir())],
        )
        ir_path = root / "ir-v1.json"
        ir_path.write_text(json.dumps(self.doc, sort_keys=True, indent=1), encoding="utf-8")
        binding_path = root / "suite-binding.yaml"
        binding_path.write_text(yaml.safe_dump(binding, sort_keys=True), encoding="utf-8")
        base: dict[str, Any] = {
            "ir_path": ir_path,
            "ir_digest": sha256_file(ir_path),
            "suite_binding_path": binding_path,
            "suite_binding_digest": sha256_file(binding_path),
            "ir_semantic_digest": sha256_canonical(self.doc),
        }
        base.update(overrides)
        return self.closure.request(**base)


@pytest.fixture
def v1(make_closure: Build) -> V1:
    return V1(make_closure([]))


def _verdicts(receipt: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {o["obligation_id"]: o for o in receipt["observation_or_outcome"]["obligations"]}


def test_identifier_matches_compiler_rule() -> None:
    assert identifier("case.pass") == "test_case_pass"
    assert identifier("case:odd/id") == "test_case_odd_id"
    assert identifier("é-x") == "test___x"


def test_v1_verdicts_come_from_outcome_record(v1: V1) -> None:
    receipt = run(v1.request())
    verify_receipt(receipt)
    o = _verdicts(receipt)
    assert o["ob/pass"]["verdict"] == "pass"
    assert {c["case_id"] for c in o["ob/pass"]["cases"]} == {"case.pass", "case:odd/id"}
    assert o["ob/fail"]["verdict"] == "fail"
    assert o["ob/held"]["verdict"] == "unresolved" and o["ob/held"]["cases"] == []
    assert receipt["observation_or_outcome"]["overall_verdict"] == "fail"
    assert receipt["evidence_grade"] == "candidate_authority_rehearsal"
    coords = receipt["governing_coordinates"]
    assert coords["canonical_authority"] == {
        "repository": "Quantum-L9/.github",
        "revision": REV,
        "admitted": False,
    }
    assert coords["conformance_ir"]["semantic_digest"] == sha256_canonical(v1.doc)
    assert coords["fixture_ir"]["digest"] == sha256_file(v1.closure.root / "fixture-ir.yaml")
    assert "semantic_digest" in coords["fixture_ir"]


def test_v1_receipt_inputs_recomputed(v1: V1) -> None:
    receipt = run(v1.request())
    root = v1.closure.root
    ir_bytes = (root / "ir-v1.json").read_bytes()
    fixture_bytes = (root / "fixture-ir.yaml").read_bytes()
    verify_receipt_inputs(receipt, ir_bytes=ir_bytes, fixture_ir_bytes=fixture_bytes)
    with pytest.raises(ReceiptInvalid, match=r"conformance_ir\.digest"):
        verify_receipt_inputs(receipt, ir_bytes=ir_bytes + b"\n", fixture_ir_bytes=fixture_bytes)


def test_v1_case_absent_from_record_is_error(v1: V1) -> None:
    v1.doc["obligations"][0] = _obligation("ob/pass", ["case.untagged"])
    receipt = run(v1.request())
    case = _verdicts(receipt)["ob/pass"]["cases"][0]
    assert case["verdict"] == "error" and "absent from outcome record" in case["detail"]


def test_v1_record_junit_disagreement_is_error(v1: V1) -> None:
    v1.write_suite(CONFTEST.replace('_OUT[case] = "fail"', '_OUT[case] = "pass"'))
    receipt = run(v1.request())
    case = _verdicts(receipt)["ob/fail"]["cases"][0]
    assert case["verdict"] == "error" and "disagrees with junit" in case["detail"]


def test_v1_missing_record_is_error(v1: V1) -> None:
    v1.write_suite(CONFTEST.replace("if path:", "if False:"))
    receipt = run(v1.request())
    case = _verdicts(receipt)["ob/pass"]["cases"][0]
    assert case["verdict"] == "error" and "no readable outcome record" in case["detail"]


def test_v1_driver_env_exported(v1: V1, tmp_path: Path) -> None:
    pkg = tmp_path / "h" / "src" / "toy_harness"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "driver.py").write_text("def create_driver():\n    return None\n", encoding="utf-8")
    v1.doc["obligations"] = [_obligation("ob/drv", ["case.driver"])]
    v1.doc["unresolved"] = []
    harness = HarnessRequest(
        root_dir=pkg,
        root="src/toy_harness",
        source_revision="wip@" + "a" * 40,
        driver_ref="toy_harness.driver:create_driver",
    )
    receipt = run(v1.request(harness=harness))
    assert _verdicts(receipt)["ob/drv"]["verdict"] == "pass"


def test_v1_closure_digest_mismatch_refused(v1: V1) -> None:
    v1.doc["subject"]["closure_digest"] = "sha256:" + "e" * 64
    with pytest.raises(RunnerRefusal, match="closure_digest"):
        run(v1.request())


def test_v1_binding_must_name_exactly_the_four_files(v1: V1) -> None:
    v1.binding["files"] = [{"path": "conftest.py", "sha256": "sha256:" + "0" * 64}]
    with pytest.raises(RunnerRefusal, match="must be exactly"):
        run(v1.request())


def test_v1_identifier_collision_refused(v1: V1) -> None:
    v1.doc["obligations"][0] = _obligation("ob/pass", ["case.pass", "case_pass"])
    with pytest.raises(RunnerRefusal, match="collide"):
        run(v1.request())


def _drop_plan_case(d: dict[str, Any]) -> None:
    d["obligations"][0]["execution_plan"]["cases"] = ["case.pass"]


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
