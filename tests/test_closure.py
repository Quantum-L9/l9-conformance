"""Compiler closures (``l9.conformance-closure/v1``) and the birth gate.

Every closure here is real compiler output (``testdata/compiled-closures/l9-state-711da68``,
regenerated and checked by ``scripts/regen_compiled_closure.py --check``), copied into a
temporary directory; refusal tests mutate the copy. The pytest/v2 case mechanics (outcome
record, JUnit cross-check) are covered by ``test_ir_v1.py``.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import run_conformance

from l9_conformance import closure
from l9_conformance.digests import sha256_canonical, sha256_file
from l9_conformance.receipt import (
    BIRTH_GATE_RULE,
    IDENTITY_PREFIX,
    ReceiptInvalid,
    birth_gate,
    body_digest,
    verify_receipt,
)
from l9_conformance.runner import ClosureRunRequest, RunnerRefusal, RunRequest, run, run_closure

FIXTURE = Path(__file__).resolve().parents[1] / "testdata/compiled-closures/l9-state-711da68"
AUTHORITY_REVISION = "31b4f4f699becb1437b4804c308650bbad60c00e"
STATIC = ("obl.STATE-DOMAIN-001", "obl.STATE-PROVIDER-001", "obl.STATE-BIRTH-001")
EXTERNAL = ("obl.STATE-BUDGET-002",)
HARNESS = "src/l9_state_conformance_harness"


def _copy(dest: Path) -> tuple[Path, Path]:
    shutil.copytree(FIXTURE, dest)
    return dest / "closure" / "closure.json", dest / "product"


@pytest.fixture
def compiled(tmp_path: Path) -> tuple[Path, Path]:
    return _copy(tmp_path / "fixture")


def _request(closure_json: Path, product: Path, **overrides: Any) -> ClosureRunRequest:
    base: dict[str, Any] = {"closure_path": closure_json, "product_root": product}
    base.update(overrides)
    return ClosureRunRequest(**base)


@pytest.fixture(scope="module")
def receipt(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    closure_json, product = _copy(tmp_path_factory.mktemp("run") / "fixture")
    return run_closure(_request(closure_json, product))


def _closure_doc(closure_json: Path) -> dict[str, Any]:
    return json.loads(closure_json.read_text(encoding="utf-8"))


def _rewrite(closure_json: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    doc = _closure_doc(closure_json)
    mutate(doc)
    closure_json.write_text(json.dumps(doc, sort_keys=True, indent=1) + "\n", encoding="utf-8")


def _obligations(receipt: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {o["obligation_id"]: o for o in receipt["observation_or_outcome"]["obligations"]}


# ── a full run of the compiled closure ───────────────────────────────────────


def test_compiled_closure_yields_a_verifiable_candidate_rehearsal_receipt(
    receipt: dict[str, Any],
) -> None:
    verify_receipt(receipt)
    doc = _closure_doc(FIXTURE / "closure" / "closure.json")
    assert receipt["evidence_grade"] == "candidate_authority_rehearsal"
    assert receipt["authority"] == {
        "grants_semantic_authority": False,
        "grants_execution_authority": False,
        "is_admission": False,
    }
    subject = receipt["exact_subject_ref"]
    assert subject["product_ref"] == "l9.state"
    assert subject["node_artifact"]["manifest_digest"] == doc["artifact_manifest_digest"]
    assert subject["implementation"]["source_revision"] == "711da68"
    coordinates = receipt["governing_coordinates"]
    assert coordinates["canonical_authority"] == {
        "repository": "Quantum-L9/.github",
        "revision": AUTHORITY_REVISION,
        "admitted": False,
    }
    assert coordinates["conformance_ir"]["digest"] == doc["ir_digest"]
    assert coordinates["fixture_ir"]["semantic_digest"] == doc["fixture_ir_digest"]
    closure_block = coordinates["conformance_closure"]
    assert closure_block["digest"] == sha256_file(FIXTURE / "closure" / "closure.json")
    assert closure_block["authority_status"] == "candidate_not_admitted"
    assert {s["ref"] for s in coordinates["semantic_source_digests"]} == {
        "l9.conformance-ir/v1",
        "l9.fixture-ir/v1",
        "l9.harness-surface/v1",
    }
    assert coordinates["profiles"] == []


def test_harness_is_bound_per_ruling_d5_with_the_closure_driver(receipt: dict[str, Any]) -> None:
    doc = _closure_doc(FIXTURE / "closure" / "closure.json")
    bound = receipt["governing_coordinates"]["harness"]
    assert bound["root"] == HARNESS
    assert bound["driver_ref"] == doc["driver_factory"]["driver"]
    files = {f["path"]: f["sha256"] for f in bound["files"]}
    assert set(files) == {
        f"{HARNESS}/{n}" for n in ("__init__.py", "controls.py", "driver.py", "observations.py")
    }
    assert files[doc["driver_factory"]["module_path"]] == doc["driver_factory"]["digest"]


def test_every_ir_obligation_is_reported_and_none_unresolved_is_executed(
    receipt: dict[str, Any],
) -> None:
    conformance_ir = json.loads((FIXTURE / "closure/conformance-ir.json").read_text())
    by_id = _obligations(receipt)
    assert list(by_id) == [o["id"] for o in conformance_ir["obligations"]]
    for source in conformance_ir["obligations"]:
        out = by_id[source["id"]]
        assert out["required"] is True
        assert out["verification_class"] == source["verification_class"]
        assert out["governing_laws"] == [
            {"law_ref": ref, "verification_class": source["verification_class"]}
            for ref in source["governing_refs"]
        ]
        if source["disposition"] == "unresolved":
            assert out["verdict"] == "unresolved"
            assert out["cases"] == []
        else:
            assert sorted(c["case_id"] for c in out["cases"]) == sorted(
                source["execution_plan"]["cases"]
            )


def test_compiled_node_cannot_import_the_harness_so_every_case_is_an_error(
    receipt: dict[str, Any],
) -> None:
    """The compiled node lacks l9_state/retention.py (PROVENANCE.md): evidence, never a pass.

    Every case's detail is the suite's own outcome record for exactly one selected test,
    which shows per-case selection and the outcome record are wired end to end.
    """
    cases = [c for o in _obligations(receipt).values() for c in o["cases"]]
    assert cases
    assert {c["verdict"] for c in cases} == {"error"}
    assert {c["detail"] for c in cases} == {"outcome record: error"}
    assert receipt["observation_or_outcome"]["overall_verdict"] == "error"


def test_state_birth_gate_is_not_passable_and_lists_open_evidence(
    receipt: dict[str, Any],
) -> None:
    gate = receipt["observation_or_outcome"]["birth_gate"]
    assert gate["birth_passable"] is False
    assert gate["rule"] == BIRTH_GATE_RULE
    assert [o["obligation_id"] for o in gate["open_evidence_obligations"]] == [
        o for o in _obligations(receipt) if o in (*STATIC, *EXTERNAL)
    ]
    assert any("verdict error" in r for r in gate["reasons"])
    # IR-unresolved fixture_replay obligations are never passable.
    assert any(
        r.startswith("obl.STATE-IDEMP-001: unresolved under a fixture_replay law")
        for r in gate["reasons"]
    )
    for oid in (*STATIC, *EXTERNAL):
        assert _obligations(receipt)[oid]["verdict"] == "unresolved"  # never rewritten


def test_cli_closure_mode_writes_receipt_and_exits_non_pass(
    compiled: tuple[Path, Path], tmp_path: Path
) -> None:
    closure_json, product = compiled
    out = tmp_path / "receipt.json"
    argv = ["--closure", str(closure_json), "--product-root", str(product), "--out", str(out)]
    assert run_conformance.main(argv) == 1
    verify_receipt(json.loads(out.read_text()))


@pytest.mark.parametrize(
    "extra",
    [
        ["--ir", "x.json"],
        ["--harness-dir", "h"],
        [],  # no --product-root
    ],
)
def test_cli_closure_mode_refuses_mixed_or_incomplete_inputs(
    compiled: tuple[Path, Path], tmp_path: Path, extra: list[str]
) -> None:
    closure_json, _ = compiled
    argv = ["--closure", str(closure_json), "--out", str(tmp_path / "r"), *extra]
    with pytest.raises(SystemExit) as exc:
        run_conformance.main(argv)
    assert exc.value.code == 2
    assert not (tmp_path / "r").exists()


# ── fail closed: every binding the closure names ─────────────────────────────


def _refused(closure_json: Path, product: Path, match: str, **overrides: Any) -> None:
    request = _request(closure_json, product, **overrides)
    with pytest.raises(RunnerRefusal, match=match):
        run_closure(request)


def _set(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(doc: dict[str, Any]) -> None:
        doc[key] = value

    return mutate


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (_set("schema", "l9.conformance-closure/v2"), "is not l9.conformance-closure/v1"),
        (_set("suite_binding", "pytest/v1"), "suite_binding"),
        (_set("authority_status", "admitted"), "not interpretable"),
        (_set("ir_digest", "sha256:" + "0" * 64), "conformance IR digest mismatch"),
        (_set("fixture_ir_digest", "sha256:" + "0" * 64), "fixture_ir_digest is not"),
        (_set("artifact_manifest_digest", "sha256:" + "0" * 64), "manifest digest mismatch"),
        (_set("product_ref", "l9.other"), "product_ref"),
        (_set("ir", "../closure/conformance-ir.json"), "relative and contained"),
        (_set("authority_revision", "Quantum-L9/.github@main"), "authority_revision"),
        (_set("profile_refs", ["l9.fixture-profile/unknown@1"]), "profile_refs differ"),
        (_set("source_revision", ""), "source_revision"),
    ],
)
def test_closure_field_tampering_refuses(
    compiled: tuple[Path, Path], mutate: Callable[[dict[str, Any]], None], match: str
) -> None:
    closure_json, product = compiled
    _rewrite(closure_json, mutate)
    _refused(closure_json, product, match)


def test_suite_file_tampering_refuses(compiled: tuple[Path, Path]) -> None:
    closure_json, product = compiled
    test_file = closure_json.parent / "suite" / "test_l9_conformance.py"
    test_file.write_text(test_file.read_text() + "\n# drift\n")
    _refused(closure_json, product, "rendered suite: digest mismatch")


def test_unbound_suite_file_refuses(compiled: tuple[Path, Path]) -> None:
    closure_json, product = compiled
    (closure_json.parent / "suite" / "test_extra.py").write_text("def test_x():\n    pass\n")
    _refused(closure_json, product, "unbound files present")


def test_conformance_ir_byte_change_refuses(compiled: tuple[Path, Path]) -> None:
    closure_json, product = compiled
    path = closure_json.parent / "conformance-ir.json"
    path.write_bytes(path.read_bytes() + b"\n")
    _refused(closure_json, product, "conformance IR digest mismatch")


def test_fixture_ir_semantic_change_refuses(compiled: tuple[Path, Path]) -> None:
    closure_json, product = compiled
    path = closure_json.parent / "fixture-ir.json"
    doc = json.loads(path.read_text())
    doc["fixture_cases"].pop()
    path.write_text(json.dumps(doc))
    _refused(closure_json, product, "fixture IR digest mismatch")


def test_fixture_ir_reformatting_is_not_a_semantic_change(compiled: tuple[Path, Path]) -> None:
    """The closure binds the fixture IR's semantic digest, as the conformance IR does."""
    closure_json, _ = compiled
    path = closure_json.parent / "fixture-ir.json"
    before = sha256_canonical(json.loads(path.read_text()))
    path.write_text(json.dumps(json.loads(path.read_text()), indent=4))
    bound = closure.load(closure_json)
    assert bound.fixture_ir_digest == before
    assert closure.law_index(bound)["STATE-DOMAIN-001"] == "static_inspection"


def test_node_artifact_tampering_refuses(compiled: tuple[Path, Path]) -> None:
    closure_json, product = compiled
    service = closure_json.parent / "node" / "src" / "l9_state" / "service.py"
    service.write_text(service.read_text() + "\n# drift\n")
    _refused(closure_json, product, "node artifact: digest mismatch")


def test_node_birth_artifact_tampering_refuses(compiled: tuple[Path, Path]) -> None:
    closure_json, product = compiled
    readme = closure_json.parent / "node" / "README.md"
    readme.write_text(readme.read_text() + "\ndrift\n")
    _refused(closure_json, product, r"node artifact: digest mismatch for README\.md")


def test_driver_module_tampering_refuses(compiled: tuple[Path, Path]) -> None:
    closure_json, product = compiled
    driver = product / HARNESS / "driver.py"
    driver.write_text(driver.read_text() + "\n# drift\n")
    _refused(closure_json, product, "driver module digest mismatch")


def test_missing_harness_refuses(compiled: tuple[Path, Path], tmp_path: Path) -> None:
    closure_json, _ = compiled
    empty = tmp_path / "empty-product"
    empty.mkdir()
    _refused(closure_json, empty, "harness root directory .* is missing")


def test_harness_symlink_refuses(compiled: tuple[Path, Path], tmp_path: Path) -> None:
    closure_json, product = compiled
    outside = tmp_path / "outside.py"
    outside.write_text("X = 1\n")
    (product / HARNESS / "extra.py").symlink_to(outside)
    _refused(closure_json, product, "symlink")


def test_harness_bytecode_refuses(compiled: tuple[Path, Path]) -> None:
    closure_json, product = compiled
    (product / HARNESS / "__pycache__").mkdir()
    _refused(closure_json, product, "bytecode directory")


def test_driver_module_must_be_the_file_the_spec_imports(compiled: tuple[Path, Path]) -> None:
    closure_json, product = compiled

    def mutate(doc: dict[str, Any]) -> None:
        doc["driver_factory"]["driver"] = "l9_state_conformance_harness.controls:create_driver"

    _rewrite(closure_json, mutate)
    _refused(closure_json, product, "is not the file")


def test_source_revision_must_match_the_closure(compiled: tuple[Path, Path]) -> None:
    closure_json, product = compiled
    _refused(closure_json, product, "source revision mismatch", source_revision="0" * 40)


def _rebind_ir(closure_json: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    """Edit the IR consistently: rewrite its bytes and re-bind its digest in the closure."""
    ir_path = closure_json.parent / "conformance-ir.json"
    doc = json.loads(ir_path.read_text())
    mutate(doc)
    ir_path.write_text(json.dumps(doc, sort_keys=True, indent=1) + "\n")
    _rewrite(closure_json, _set("ir_digest", sha256_file(ir_path)))


def test_ir_unresolved_list_must_cover_unresolved_obligations(compiled: tuple[Path, Path]) -> None:
    closure_json, product = compiled

    def mutate(doc: dict[str, Any]) -> None:
        doc["unresolved"] = [u for u in doc["unresolved"] if u["obligation_ref"] not in STATIC]

    _rebind_ir(closure_json, mutate)
    _refused(closure_json, product, "not listed in ir.unresolved")


def test_ir_cannot_promote_a_static_inspection_obligation(compiled: tuple[Path, Path]) -> None:
    closure_json, product = compiled

    def mutate(doc: dict[str, Any]) -> None:
        for o in doc["obligations"]:
            if o["id"] == STATIC[0]:
                o["disposition"] = "executable"
        doc["unresolved"] = [u for u in doc["unresolved"] if u["obligation_ref"] != STATIC[0]]

    _rebind_ir(closure_json, mutate)
    _refused(closure_json, product, "executable requires fixture_replay")


def test_ir_cannot_relabel_a_law_verification_class(compiled: tuple[Path, Path]) -> None:
    """An obligation's class is its law's; relabelling one to dodge the birth gate refuses."""
    closure_json, product = compiled

    def mutate(doc: dict[str, Any]) -> None:
        for o in doc["obligations"]:
            if o["id"] == "obl.STATE-IDEMP-001":
                o["verification_class"] = "static_inspection"
                o.pop("execution_plan", None)

    _rebind_ir(closure_json, mutate)
    _refused(closure_json, product, "is not its law STATE-IDEMP-001's fixture_replay")


def test_bare_v1_ir_needs_its_closure_or_a_suite_binding(compiled: tuple[Path, Path]) -> None:
    closure_json, _ = compiled
    doc = _closure_doc(closure_json)
    base = closure_json.parent
    request = RunRequest(
        ir_path=base / doc["ir"],
        ir_digest=doc["ir_digest"],
        fixture_ir_path=base / doc["fixture_ir"],
        suite_dir=base / doc["suite_dir"],
        artifact_dir=base / doc["artifact_dir"],
        artifact_manifest_path=base / doc["artifact_manifest"],
        artifact_manifest_digest=doc["artifact_manifest_digest"],
        source_revision=doc["source_revision"],
    )
    with pytest.raises(RunnerRefusal, match="requires a digest-bound suite binding"):
        run(request)


def test_receipt_birth_gate_is_re_derived(receipt: dict[str, Any]) -> None:
    """A correctly sealed receipt whose birth gate is not its obligations' is refused."""
    forged = json.loads(json.dumps(receipt))
    forged["observation_or_outcome"]["birth_gate"]["birth_passable"] = True
    body = {k: v for k, v in forged.items() if k not in ("receipt_identity", "receipt_digest")}
    forged["receipt_digest"] = body_digest(body)
    forged["receipt_identity"] = IDENTITY_PREFIX + forged["receipt_digest"]
    with pytest.raises(ReceiptInvalid, match="birth_gate"):
        verify_receipt(forged)


# ── the birth-gate rule (CTO ruling) ──────────────────────────────────────────


def _ob(oid: str, verdict: str, *classes: str | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"obligation_id": oid, "verdict": verdict}
    if classes:
        entry["governing_laws"] = [
            {"law_ref": f"{oid}-L{i}", "verification_class": c} for i, c in enumerate(classes)
        ]
    return entry


def test_birth_gate_passes_with_only_static_and_external_evidence_open() -> None:
    gate = birth_gate(
        [
            _ob("a", "pass", "fixture_replay"),
            _ob("s", "unresolved", "static_inspection"),
            _ob("e", "unresolved", "external_runtime_evidence"),
        ]
    )
    assert gate["birth_passable"] is True
    assert gate["reasons"] == []
    assert [o["obligation_id"] for o in gate["open_evidence_obligations"]] == ["s", "e"]


@pytest.mark.parametrize(
    ("obligation", "reason"),
    [
        (_ob("f", "fail", "fixture_replay"), "f: verdict fail"),
        (_ob("x", "error", "fixture_replay"), "x: verdict error"),
        (_ob("u", "unresolved", "fixture_replay"), "u: unresolved under a fixture_replay law"),
        (_ob("n", "unresolved", None), "n: unresolved and not mapped"),
        (_ob("m", "unresolved"), "m: unresolved and not mapped"),
        (
            _ob("mix", "unresolved", "static_inspection", "fixture_replay"),
            "mix: unresolved under a fixture_replay/static_inspection law",
        ),
    ],
)
def test_birth_gate_refuses(obligation: dict[str, Any], reason: str) -> None:
    gate = birth_gate([_ob("ok", "pass", "fixture_replay"), obligation])
    assert gate["birth_passable"] is False
    assert len(gate["reasons"]) == 1
    assert gate["reasons"][0].startswith(reason)


def test_birth_gate_with_no_obligations_is_not_passable() -> None:
    assert birth_gate([])["birth_passable"] is False
