"""Runner verdict mapping and fail-closed refusal (synthetic closures only)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import Closure, ObligationSpec, replay

from l9_conformance.digests import sha256_file
from l9_conformance.receipt import verify_receipt
from l9_conformance.runner import RunnerRefusal, run

Build = Callable[[list[ObligationSpec]], Closure]


def _verdicts(receipt: dict[str, Any]) -> dict[str, str]:
    return {
        o["obligation_id"]: o["verdict"] for o in receipt["observation_or_outcome"]["obligations"]
    }


def test_all_cases_pass_yields_pass_receipt(make_closure: Build) -> None:
    receipt = run(make_closure([replay("ob/a", "test_pass")]).request())
    verify_receipt(receipt)
    assert receipt["observation_or_outcome"]["overall_verdict"] == "pass"
    assert receipt["evidence_grade"] == "synthetic_test"
    assert receipt["authority"] == {
        "grants_semantic_authority": False,
        "grants_execution_authority": False,
        "is_admission": False,
    }
    record = receipt["verification_basis"]["case_records"][0]
    assert record["exit_code"] == 0 and record["junit_digest"].startswith("sha256:")


@pytest.mark.parametrize(
    ("test_name", "expected"),
    [
        ("test_fail", "fail"),
        ("test_error", "error"),
        ("test_skip", "unresolved"),
        ("test_does_not_exist", "unresolved"),
        ("test_param", "unresolved"),  # nodeid matches two tests: ambiguous, never pass
    ],
)
def test_case_outcome_mapping(make_closure: Build, test_name: str, expected: str) -> None:
    receipt = run(make_closure([replay("ob/a", test_name)]).request())
    verify_receipt(receipt)
    assert _verdicts(receipt) == {"ob/a": expected}
    assert receipt["observation_or_outcome"]["overall_verdict"] == expected


def test_one_failing_case_fails_obligation(make_closure: Build) -> None:
    receipt = run(make_closure([replay("ob/a", "test_pass", "test_fail")]).request())
    assert _verdicts(receipt) == {"ob/a": "fail"}
    assert receipt["observation_or_outcome"]["overall_verdict"] == "fail"


def test_overall_precedence_fail_over_error_over_unresolved(make_closure: Build) -> None:
    receipt = run(
        make_closure(
            [replay("ob/u", "test_skip"), replay("ob/e", "test_error"), replay("ob/f", "test_fail")]
        ).request()
    )
    assert receipt["observation_or_outcome"]["overall_verdict"] == "fail"


def test_optional_obligation_does_not_gate_overall(make_closure: Build) -> None:
    receipt = run(
        make_closure(
            [replay("ob/req", "test_pass"), replay("ob/opt", "test_fail", required=False)]
        ).request()
    )
    assert _verdicts(receipt) == {"ob/req": "pass", "ob/opt": "fail"}
    assert receipt["observation_or_outcome"]["overall_verdict"] == "pass"


def test_obligation_without_cases_is_unresolved(make_closure: Build) -> None:
    receipt = run(make_closure([replay("ob/empty")]).request())
    assert _verdicts(receipt) == {"ob/empty": "unresolved"}
    assert receipt["observation_or_outcome"]["overall_verdict"] == "unresolved"


def test_no_obligations_is_unresolved_not_pass(make_closure: Build) -> None:
    receipt = run(make_closure([]).request())
    assert receipt["observation_or_outcome"]["overall_verdict"] == "unresolved"


@pytest.mark.parametrize("vclass", ["static_inspection", "external_runtime_evidence"])
def test_non_replay_classes_are_unresolved(make_closure: Build, vclass: str) -> None:
    obligation = replay("ob/x", "test_pass")
    obligation["verification_class"] = vclass
    receipt = run(make_closure([obligation]).request())
    assert _verdicts(receipt) == {"ob/x": "unresolved"}
    assert receipt["observation_or_outcome"]["overall_verdict"] == "unresolved"


def test_timeout_is_error(make_closure: Build) -> None:
    closure = make_closure([replay("ob/a", "test_pass")])
    receipt = run(closure.request(timeout_seconds=0.001))
    assert _verdicts(receipt) == {"ob/a": "error"}


# --- refusals: no receipt is produced -------------------------------------------------


def test_ir_digest_mismatch_refused(make_closure: Build) -> None:
    closure = make_closure([replay("ob/a", "test_pass")])
    with pytest.raises(RunnerRefusal, match="conformance IR digest mismatch"):
        run(closure.request(ir_digest="sha256:" + "f" * 64))


def test_suite_digest_mismatch_refused(make_closure: Build) -> None:
    closure = make_closure([replay("ob/a", "test_pass")])
    request = closure.request()
    (closure.root / "suite" / "test_toy.py").write_text(
        "def test_pass():\n    pass\n", encoding="utf-8"
    )
    with pytest.raises(RunnerRefusal, match="rendered suite: digest mismatch"):
        run(request)


def test_unbound_suite_file_refused(make_closure: Build) -> None:
    closure = make_closure([replay("ob/a", "test_pass")])
    (closure.root / "suite" / "conftest.py").write_text("", encoding="utf-8")
    with pytest.raises(RunnerRefusal, match="unbound files present"):
        run(closure.request())


def test_missing_artifact_file_refused(make_closure: Build) -> None:
    closure = make_closure([replay("ob/a", "test_pass")])
    (closure.root / "artifact" / "toy_node.py").unlink()
    with pytest.raises(RunnerRefusal, match="bound files missing"):
        run(closure.request())


def test_missing_artifact_dir_refused(make_closure: Build) -> None:
    closure = make_closure([replay("ob/a", "test_pass")])
    with pytest.raises(RunnerRefusal, match=r"directory .* is missing"):
        run(closure.request(artifact_dir=closure.root / "nope"))


def test_artifact_byte_change_refused(make_closure: Build) -> None:
    closure = make_closure([replay("ob/a", "test_pass")])
    (closure.root / "artifact" / "toy_node.py").write_text(
        "def add(a, b):\n    return 3\n", encoding="utf-8"
    )
    with pytest.raises(RunnerRefusal, match="node artifact: digest mismatch"):
        run(closure.request())


def test_artifact_manifest_digest_mismatch_refused(make_closure: Build) -> None:
    closure = make_closure([replay("ob/a", "test_pass")])
    with pytest.raises(RunnerRefusal, match="artifact manifest digest mismatch"):
        run(closure.request(artifact_manifest_digest="sha256:" + "e" * 64))


def test_fixture_ir_digest_mismatch_refused(make_closure: Build) -> None:
    closure = make_closure([replay("ob/a", "test_pass")])
    (closure.root / "fixture-ir.yaml").write_text("tampered: true\n", encoding="utf-8")
    with pytest.raises(RunnerRefusal, match="fixture IR digest mismatch"):
        run(closure.request())


def test_unresolvable_profile_refused(make_closure: Build) -> None:
    closure = make_closure([replay("ob/a", "test_pass")])
    closure.ir_doc["profiles"] = [
        {"ref": "l9.fixture-profile/nope@1", "digest": "sha256:" + "a" * 64}
    ]
    with pytest.raises(RunnerRefusal, match="does not resolve"):
        run(closure.request())


def test_profile_digest_mismatch_refused(make_closure: Build) -> None:
    closure = make_closure([replay("ob/a", "test_pass")])
    closure.ir_doc["profiles"] = [
        {"ref": "l9.fixture-profile/core@1", "digest": "sha256:" + "a" * 64}
    ]
    with pytest.raises(RunnerRefusal, match="digest mismatch"):
        run(closure.request())


def test_resolvable_profile_is_bound(make_closure: Build) -> None:
    root = Path(__file__).resolve().parents[1]
    digest = sha256_file(root / "catalog" / "fixture-profile-core-1.yaml")
    closure = make_closure([replay("ob/a", "test_pass")])
    closure.ir_doc["profiles"] = [{"ref": "l9.fixture-profile/core@1", "digest": digest}]
    receipt = run(closure.request())
    assert receipt["governing_coordinates"]["profiles"] == [
        {"ref": "l9.fixture-profile/core@1", "digest": digest}
    ]


def test_case_outside_bound_suite_refused(make_closure: Build) -> None:
    obligation = replay("ob/a", "test_pass")
    obligation["cases"][0]["nodeid"] = "../escape.py::test_x"
    with pytest.raises(RunnerRefusal, match="not a plain contained pytest node id"):
        run(make_closure([obligation]).request())


def _set(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    return lambda d: d.update({key: value})


def _set_plan_isolation(d: dict[str, Any]) -> None:
    d["execution_plan"]["isolation"] = "shared"


def _bad_vclass(d: dict[str, Any]) -> None:
    d["obligations"][0]["verification_class"] = "vibes"


def _duplicate_obligation(d: dict[str, Any]) -> None:
    d["obligations"].append(dict(d["obligations"][0]))


IR_MUTATIONS: list[tuple[Callable[[dict[str, Any]], None], str]] = [
    (_set("schema", "l9.conformance-ir/v999"), "not a supported"),
    (_set("synthetic", False), "synthetic: true"),
    (_set_plan_isolation, "not supported"),
    (_bad_vclass, "closed set"),
    (_duplicate_obligation, "duplicated"),
]


@pytest.mark.parametrize(("mutate", "match"), IR_MUTATIONS)
def test_invalid_ir_refused(
    make_closure: Build, mutate: Callable[[dict[str, Any]], None], match: str
) -> None:
    closure = make_closure([replay("ob/a", "test_pass")])
    mutate(closure.ir_doc)
    with pytest.raises(RunnerRefusal, match=match):
        run(closure.request())


def test_case_runs_in_fresh_directory(make_closure: Build, tmp_path: Path) -> None:
    """A case that writes state must not leak it into the next case."""
    closure = make_closure([])
    suite = closure.root / "suite" / "test_toy.py"
    suite.write_text(
        "import os\n\n"
        "def test_first():\n"
        "    assert not os.path.exists('marker')\n"
        "    open('marker', 'w').close()\n\n"
        "def test_second():\n"
        "    assert not os.path.exists('marker')\n",
        encoding="utf-8",
    )
    closure.ir_doc["rendered_suite"]["files"][0]["sha256"] = sha256_file(suite)
    closure.ir_doc["obligations"] = [replay("ob/iso", "test_first", "test_second")]
    receipt = run(closure.request())
    assert _verdicts(receipt) == {"ob/iso": "pass"}
    assert not (tmp_path / "suite" / "marker").exists()


def _cli_args(closure: Closure, out: Path, **overrides: str) -> list[str]:
    request = closure.request()
    assert request.bound_source_digest is not None
    args = {
        "--ir": str(request.ir_path),
        "--ir-digest": request.ir_digest,
        "--fixture-ir": str(request.fixture_ir_path),
        "--suite-dir": str(request.suite_dir),
        "--artifact-dir": str(request.artifact_dir),
        "--artifact-manifest": str(request.artifact_manifest_path),
        "--artifact-manifest-digest": request.artifact_manifest_digest,
        "--source-revision": request.source_revision,
        "--bound-source-digest": request.bound_source_digest,
        "--out": str(out),
    }
    args.update(overrides)
    return [part for pair in args.items() for part in pair]


def test_cli_exit_codes(make_closure: Build, tmp_path: Path) -> None:
    import json

    from run_conformance import main

    closure = make_closure([replay("ob/a", "test_pass")])
    out = tmp_path / "receipt.json"
    assert main(_cli_args(closure, out)) == 0
    verify_receipt(json.loads(out.read_text(encoding="utf-8")))
    refused = tmp_path / "refused.json"
    assert main(_cli_args(closure, refused, **{"--ir-digest": "sha256:" + "0" * 64})) == 2
    assert not refused.exists()


def test_grade_rules(make_closure: Build) -> None:
    """Grade never depends on isolation level (D3); it depends on authority and profiles."""
    from dataclasses import replace

    from l9_conformance import ir
    from l9_conformance.runner import grade

    closure = make_closure([replay("ob/a", "test_pass")])
    base = ir.parse(closure.ir_doc)
    assert grade(base, ()) == "synthetic_test"
    real = replace(base, synthetic=False)
    assert grade(real, ()) == "candidate_authority_rehearsal"
    admitted = replace(real, canonical_admitted=True)
    assert grade(admitted, ()) == "canonical"
    assert grade(admitted, ("l9.fixture-profile/x@1",)) == "candidate_authority_rehearsal"


def test_isolation_recorded_honestly(make_closure: Build) -> None:
    receipt = run(make_closure([replay("ob/a", "test_pass")]).request())
    isolation = receipt["verification_basis"]["isolation"]
    assert isolation["level"] == "process"
    assert isolation["network"] == "best_effort_unrestricted"
    assert isolation["requested"] == "fresh_subject_per_case"


@pytest.mark.parametrize(
    "nodeid",
    ["-p evil::test_x", "test_toy.py", "test_toy.py::test_pass; rm -rf /", "/abs/test_toy.py::t"],
)
def test_option_like_or_malformed_nodeid_refused(make_closure: Build, nodeid: str) -> None:
    obligation = replay("ob/a", "test_pass")
    obligation["cases"][0]["nodeid"] = nodeid
    with pytest.raises(RunnerRefusal, match="not a plain contained pytest node id"):
        run(make_closure([obligation]).request())


def test_case_in_bound_suite_must_exist_in_suite(make_closure: Build) -> None:
    obligation = replay("ob/a", "test_pass")
    obligation["cases"][0]["nodeid"] = "other_file.py::test_x"
    with pytest.raises(RunnerRefusal, match="not in the bound suite"):
        run(make_closure([obligation]).request())


def test_non_executable_interpreter_refused(make_closure: Build) -> None:
    closure = make_closure([replay("ob/a", "test_pass")])
    with pytest.raises(RunnerRefusal, match="case interpreter"):
        run(closure.request(python_executable="python3"))


def test_unsafe_junit_xml_is_error_not_pass(tmp_path: Path) -> None:
    from l9_conformance.runner import _classify_junit  # pyright: ignore[reportPrivateUsage]

    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<?xml version="1.0"?>\n<!DOCTYPE x [<!ENTITY a "aaaa">]>\n'
        '<testsuites><testsuite><testcase name="t"/></testsuite></testsuites>\n',
        encoding="utf-8",
    )
    verdict, detail = _classify_junit(junit, 0)
    assert verdict == "error"
    assert "unsafe XML" in detail


def test_duplicate_suite_path_refused(make_closure: Build) -> None:
    closure = make_closure([replay("ob/a", "test_pass")])
    files = closure.ir_doc["rendered_suite"]["files"]
    files.append({"path": "./test_toy.py", "sha256": "sha256:" + "c" * 64})
    with pytest.raises(RunnerRefusal, match="bound more than once"):
        run(closure.request())


def test_profile_file_bytes_must_match(make_closure: Build, tmp_path: Path) -> None:
    """The catalog index asserting the right digest is not enough; the file is hashed."""
    import shutil

    root = Path(__file__).resolve().parents[1]
    catalog_root = tmp_path / "catalog-copy"
    shutil.copytree(root / "catalog", catalog_root / "catalog")
    profile = catalog_root / "catalog" / "fixture-profile-core-1.yaml"
    digest = sha256_file(profile)
    profile.write_text(profile.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")
    closure = make_closure([replay("ob/a", "test_pass")])
    closure.ir_doc["profiles"] = [{"ref": "l9.fixture-profile/core@1", "digest": digest}]
    request = closure.request(catalog_index=catalog_root / "catalog" / "INDEX.yaml")
    with pytest.raises(RunnerRefusal, match="file digest mismatch"):
        run(request)


def test_host_pytest_plugins_not_autoloaded(make_closure: Build) -> None:
    """A case observes PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 in its environment."""
    closure = make_closure([])
    suite = closure.root / "suite" / "test_toy.py"
    suite.write_text(
        "import os\n\n"
        "def test_autoload_off():\n"
        "    assert os.environ.get('PYTEST_DISABLE_PLUGIN_AUTOLOAD') == '1'\n",
        encoding="utf-8",
    )
    closure.ir_doc["rendered_suite"]["files"][0]["sha256"] = sha256_file(suite)
    closure.ir_doc["obligations"] = [replay("ob/plug", "test_autoload_off")]
    receipt = run(closure.request())
    assert _verdicts(receipt) == {"ob/plug": "pass"}
