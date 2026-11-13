"""Conformance runner: verify every bound digest, execute each case in isolation, emit a receipt.

Order of operations is fixed and fail-closed:

1. Verify the conformance IR bytes against the caller-supplied digest, then parse.
2. Verify the fixture IR bytes against the digest the conformance IR binds.
3. Resolve every profile ref against this repository's catalog and verify its digest.
4. Verify the rendered suite directory holds exactly the files the IR binds, byte-exact.
5. Verify the node artifact manifest digest, then that the artifact directory holds
   exactly the manifest's files, byte-exact.

Any mismatch, missing file, or unexpected file raises :class:`RunnerRefusal` and no
receipt is produced. Only then is peer-supplied suite code executed, one case per
fresh temporary directory and fresh interpreter process.

Verdict mapping never yields a false pass: a case passes only when pytest reports
exactly one collected test with no failure, error, or skip and a zero exit status.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import yaml
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError
from defusedxml.ElementTree import parse as parse_xml

from l9_conformance import RUNNER_ID, RUNNER_VERSION, harness, ir, ir_v1, node_artifact
from l9_conformance.digests import file_set_digest, sha256_bytes, sha256_canonical, sha256_file
from l9_conformance.receipt import (
    AUTHORITY,
    CLAIM,
    RECEIPT_TYPE,
    SCHEMA_ID,
    Verdict,
    aggregate,
    finalize,
    overall_from_obligations,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
CATALOG_INDEX: Final = REPO_ROOT / "catalog" / "INDEX.yaml"
PACKAGE_DIR: Final = Path(__file__).resolve().parent
SCHEMA_FILE: Final = REPO_ROOT / "schemas" / "conformance-receipt-1.schema.json"

# Operator decision D3 (relayed 2026-11-12): process-level isolation is accepted for
# rehearsal and canonical grade; the receipt records exactly what was achieved.
ISOLATION_ACHIEVED: Final = "fresh_tempdir_fresh_process_scrubbed_env"
ISOLATION_LEVEL: Final = "process"
ISOLATION_NETWORK: Final = "best_effort_unrestricted"
ISOLATION_LIMITATIONS: Final = (
    "no container, network namespace, or filesystem sandbox",
    "network is not blocked; proxy and credential environment variables are not passed to cases",
)
PYTEST_NO_TESTS_COLLECTED: Final = 5


class RunnerRefusal(RuntimeError):
    """A bound input could not be verified. No receipt is emitted."""


@dataclass(frozen=True)
class RunRequest:
    ir_path: Path
    ir_digest: str
    fixture_ir_path: Path
    suite_dir: Path
    artifact_dir: Path
    artifact_manifest_path: Path
    artifact_manifest_digest: str
    source_revision: str
    bound_source_digest: str | None = None
    timeout_seconds: float = 120.0
    python_executable: str = sys.executable
    catalog_index: Path = CATALOG_INDEX
    reproduction: str = "python3 scripts/run_conformance.py <bound inputs>"
    harness: harness.HarnessRequest | None = None
    # l9.conformance-ir/v1 only: the digest-bound suite binding and the IR's expected
    # semantic digest (canonical JSON), both required for a v1 IR.
    suite_binding_path: Path | None = None
    suite_binding_digest: str | None = None
    ir_semantic_digest: str | None = None
    # Governing semantic source files used by the run (e.g. the F1 schemas), as
    # (ref, local path). Each is hashed by raw bytes and recorded; refs must be unique.
    semantic_sources: tuple[tuple[str, Path], ...] = ()


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    verdict: Verdict
    detail: str
    exit_code: int | None
    junit_digest: str | None


@dataclass
class _Verified:
    conformance_ir: ir.ConformanceIR
    artifact: node_artifact.NodeArtifact
    bound_source_digest: str
    unpublished_profiles: tuple[str, ...]
    harness: harness.HarnessBinding | None = None
    semantic_sources: tuple[ir.RefDigest, ...] = ()
    profiles: tuple[ir.RefDigest, ...] = ()
    fixture_raw_digest: str = ""
    fixture_semantic_digest: str | None = None


def _rel_files(root: Path) -> set[str]:
    return {
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink()
    }


def _check_exact_tree(root: Path, expected: Mapping[str, str], label: str) -> None:
    if not root.is_dir():
        raise RunnerRefusal(f"{label}: directory {root} is missing")
    for p in root.rglob("*"):
        if p.is_symlink():
            raise RunnerRefusal(f"{label}: symlink {p.relative_to(root).as_posix()} is not allowed")
    present = _rel_files(root)
    missing = sorted(set(expected) - present)
    extra = sorted(present - set(expected))
    if missing:
        raise RunnerRefusal(f"{label}: bound files missing: {missing}")
    if extra:
        raise RunnerRefusal(f"{label}: unbound files present: {extra}")
    for rel, digest in sorted(expected.items()):
        actual = sha256_file(root / rel)
        if actual != digest:
            raise RunnerRefusal(
                f"{label}: digest mismatch for {rel}: bound {digest}, actual {actual}"
            )


def _load_catalog(index: Path) -> dict[str, tuple[str, str, Path]]:
    """Profile id -> (catalog digest, catalog status, profile file path)."""
    data = cast(Mapping[str, Any], yaml.safe_load(index.read_text(encoding="utf-8")))
    profiles = cast(list[Mapping[str, Any]], data.get("profiles") or [])
    return {
        str(p["id"]): (str(p["digest"]), str(p.get("status", "")), _profile_path(index, p))
        for p in profiles
    }


def _profile_path(index: Path, entry: Mapping[str, Any]) -> Path:
    """Resolve a catalog entry path, refusing anything outside the catalog directory.

    The path is relative to the repository root (the INDEX's parent's parent). An
    absolute path, a ``..`` component, or a path that resolves (following symlinked
    parents) outside the catalog directory is refused, so the hashed "profile bytes"
    can only come from the catalog itself.
    """
    raw = entry.get("path")
    if not isinstance(raw, str) or not raw:
        raise RunnerRefusal(f"catalog entry {entry.get('id')!r} has no path")
    rel = Path(raw)
    if rel.is_absolute() or ".." in rel.parts:
        raise RunnerRefusal(f"catalog entry {entry.get('id')!r} path {raw!r} is not contained")
    catalog_dir = index.resolve().parent
    resolved = (index.resolve().parents[1] / rel).resolve()
    if not resolved.is_relative_to(catalog_dir):
        raise RunnerRefusal(
            f"catalog entry {entry.get('id')!r} path {raw!r} resolves outside {catalog_dir}"
        )
    return resolved


def verify_inputs(request: RunRequest) -> _Verified:
    """Steps 1-5. Raises RunnerRefusal on any unverifiable binding."""
    _interpreter(request.python_executable)
    if not request.ir_path.is_file():
        raise RunnerRefusal(f"conformance IR {request.ir_path} is missing")
    ir_bytes = request.ir_path.read_bytes()
    if sha256_bytes(ir_bytes) != request.ir_digest:
        raise RunnerRefusal(
            f"conformance IR digest mismatch: bound {request.ir_digest}, "
            f"actual {sha256_bytes(ir_bytes)}"
        )
    conformance_ir = _load_ir(request, ir_bytes)

    if not request.fixture_ir_path.is_file():
        raise RunnerRefusal(f"fixture IR {request.fixture_ir_path} is missing")
    fixture_raw = sha256_file(request.fixture_ir_path)
    fixture_digest = fixture_raw
    fixture_semantic: str | None = None
    if conformance_ir.fixture_digest_kind == "semantic":
        try:
            fixture_digest = sha256_canonical(yaml.safe_load(request.fixture_ir_path.read_bytes()))
            fixture_semantic = fixture_digest
        except yaml.YAMLError as exc:
            raise RunnerRefusal(f"fixture IR unparseable: {exc}") from exc
    if fixture_digest != conformance_ir.fixture_ir.digest:
        raise RunnerRefusal(
            f"fixture IR digest mismatch: bound {conformance_ir.fixture_ir.digest}, "
            f"actual {fixture_digest}"
        )

    catalog = _load_catalog(request.catalog_index)
    unpublished: list[str] = []
    resolved_profiles: list[ir.RefDigest] = []
    for profile in conformance_ir.profiles:
        if profile.ref not in catalog:
            raise RunnerRefusal(
                f"profile {profile.ref} does not resolve in the l9-conformance catalog"
            )
        catalog_digest, catalog_status, profile_path = catalog[profile.ref]
        if profile.digest == ir_v1.PROFILE_DIGEST_FROM_CATALOG:
            # A v1 profile ref carries no digest: bind the catalog's, then the file bytes.
            profile = ir.RefDigest(ref=profile.ref, digest=catalog_digest)
        if catalog_digest != profile.digest:
            raise RunnerRefusal(
                f"profile {profile.ref} digest mismatch: bound {profile.digest}, "
                f"catalog {catalog_digest}"
            )
        # The index only asserts a digest; the profile bytes themselves must match it.
        if not profile_path.is_file() or profile_path.is_symlink():
            raise RunnerRefusal(f"profile {profile.ref} file {profile_path} is missing")
        file_digest = sha256_file(profile_path)
        if file_digest != profile.digest:
            raise RunnerRefusal(
                f"profile {profile.ref} file digest mismatch: bound {profile.digest}, "
                f"file {file_digest}"
            )
        if catalog_status != "published":
            unpublished.append(profile.ref)
        resolved_profiles.append(profile)

    suite_paths = {f.path for f in conformance_ir.suite_files}
    _check_exact_tree(
        request.suite_dir, {f.path: f.sha256 for f in conformance_ir.suite_files}, "rendered suite"
    )
    if conformance_ir.closure_digest is not None:
        _check_suite_closure(request.suite_dir, conformance_ir.closure_digest)
    for obligation in conformance_ir.obligations:
        for case in obligation.cases:
            if case.nodeid.split("::", 1)[0] not in suite_paths:
                raise RunnerRefusal(
                    f"case {case.case_id}: nodeid {case.nodeid} is not in the bound suite"
                )

    try:
        artifact = node_artifact.load(
            request.artifact_dir, request.artifact_manifest_path, request.artifact_manifest_digest
        )
    except node_artifact.ArtifactRefused as exc:
        raise RunnerRefusal(str(exc)) from exc
    if artifact.product_ref is not None and artifact.product_ref != conformance_ir.product_ref:
        raise RunnerRefusal(
            f"node artifact product_ref {artifact.product_ref} is not the IR subject "
            f"{conformance_ir.product_ref}"
        )
    bound_source = artifact.bound_source_digest or request.bound_source_digest
    if bound_source is None:
        raise RunnerRefusal("bound source digest is neither derivable nor supplied")
    if request.bound_source_digest is not None and request.bound_source_digest != bound_source:
        raise RunnerRefusal(
            f"bound source digest mismatch: supplied {request.bound_source_digest}, "
            f"derived {bound_source}"
        )
    sources: list[ir.RefDigest] = list(conformance_ir.semantic_sources)
    for ref, path in request.semantic_sources:
        if not ref:
            raise RunnerRefusal("semantic source ref must not be empty")
        if not path.is_file() or path.is_symlink():
            raise RunnerRefusal(f"semantic source {ref} file {path} is missing or a symlink")
        sources.append(ir.RefDigest(ref=ref, digest=sha256_file(path)))
    refs = [s.ref for s in sources]
    if len(refs) != len(set(refs)):
        raise RunnerRefusal("semantic source refs must be unique")
    harness_binding: harness.HarnessBinding | None = None
    if request.harness is not None:
        try:
            harness_binding = harness.bind(request.harness)
        except harness.HarnessRefused as exc:
            raise RunnerRefusal(str(exc)) from exc
    return _Verified(
        conformance_ir=conformance_ir,
        artifact=artifact,
        bound_source_digest=bound_source,
        unpublished_profiles=tuple(unpublished),
        harness=harness_binding,
        semantic_sources=tuple(sources),
        profiles=tuple(resolved_profiles),
        fixture_raw_digest=fixture_raw,
        fixture_semantic_digest=fixture_semantic,
    )


def _check_suite_closure(suite_dir: Path, closure_digest: str) -> None:
    """The rendered suite's cases file must name the IR's closure (pytest/v2)."""
    cases_file = suite_dir / ir_v1.PYTEST_V2_CASES_FILE
    try:
        document = json.loads(cases_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunnerRefusal(f"rendered suite cases file unreadable: {exc}") from exc
    if not isinstance(document, dict):
        raise RunnerRefusal("rendered suite cases file is not a JSON object")
    recorded = cast(dict[str, Any], document).get("closure_digest")
    if recorded != closure_digest:
        raise RunnerRefusal(
            f"rendered suite closure_digest {recorded!r} is not the IR's {closure_digest}"
        )


def _load_ir(request: RunRequest, ir_bytes: bytes) -> ir.ConformanceIR:
    try:
        document = yaml.safe_load(ir_bytes)
    except yaml.YAMLError as exc:
        raise RunnerRefusal(f"conformance IR invalid: unparseable: {exc}") from exc
    schema = (
        cast(Mapping[str, Any], document).get("schema") if isinstance(document, Mapping) else None
    )
    try:
        if schema != ir_v1.SCHEMA:
            return ir.load(ir_bytes)
        if request.suite_binding_path is None or request.suite_binding_digest is None:
            raise RunnerRefusal("a v1 conformance IR requires a digest-bound suite binding")
        if request.ir_semantic_digest is None:
            raise RunnerRefusal("a v1 conformance IR requires its expected semantic digest")
        if not request.suite_binding_path.is_file():
            raise RunnerRefusal(f"suite binding {request.suite_binding_path} is missing")
        binding_bytes = request.suite_binding_path.read_bytes()
        if sha256_bytes(binding_bytes) != request.suite_binding_digest:
            raise RunnerRefusal(
                f"suite binding digest mismatch: bound {request.suite_binding_digest}, "
                f"actual {sha256_bytes(binding_bytes)}"
            )
        loaded = ir_v1.load(ir_bytes, ir_v1.load_suite_binding(binding_bytes))
    except ir.IRInvalid as exc:
        raise RunnerRefusal(f"conformance IR invalid: {exc}") from exc
    if loaded.semantic_digest != request.ir_semantic_digest:
        raise RunnerRefusal(
            f"conformance IR semantic digest mismatch: bound {request.ir_semantic_digest}, "
            f"actual {loaded.semantic_digest}"
        )
    return loaded


def _classify_junit(junit: Path, exit_code: int) -> tuple[Verdict, str]:
    try:
        # The report is written by peer-supplied suite code: parse it as untrusted XML.
        root = parse_xml(junit, forbid_dtd=True).getroot()
    except DefusedXmlException as exc:
        return "error", f"junit report rejected as unsafe XML: {type(exc).__name__}"
    except (ParseError, OSError) as exc:
        return "error", f"junit report unreadable: {exc}"
    if root is None:
        return "error", "junit report has no root element"
    testcases = list(root.iter("testcase"))
    if exit_code == PYTEST_NO_TESTS_COLLECTED or not testcases:
        return "unresolved", "no test collected for case nodeid"
    if len(testcases) != 1:
        return (
            "unresolved",
            f"case nodeid matched {len(testcases)} tests; a case must be exactly one test",
        )
    tc = testcases[0]
    if tc.find("failure") is not None:
        return "fail", "assertion failed"
    if tc.find("error") is not None:
        return "error", "test errored"
    if tc.find("skipped") is not None:
        return "unresolved", "test skipped or xfailed; not executed to a verdict"
    if exit_code != 0:
        return "error", f"pytest exited {exit_code} with no recorded failure"
    return "pass", "passed"


def _interpreter(executable: str) -> str:
    """The case interpreter must be an existing absolute executable file."""
    path = Path(executable)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise RunnerRefusal(f"case interpreter {executable!r} is not an absolute executable file")
    return str(path)


_RECORD_VERDICTS: Final = frozenset({"pass", "fail", "error", "unresolved"})


def _classify_outcome_record(
    path: Path, case_id: str, junit_verdict: Verdict
) -> tuple[Verdict, str]:
    """pytest/v2: the suite's outcome record is the verdict source; JUnit cross-checks.

    The record is keyed by the exact case id. A missing record, a missing key, a value
    outside the closed set, or disagreement with JUnit on whether the case passed is an
    error, never a pass.
    """
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "error", "no readable outcome record"
    if not isinstance(record, dict):
        return "error", "outcome record is not a JSON object"
    value = cast(dict[str, Any], record).get(case_id)
    if value is None:
        return "error", "case absent from outcome record"
    if value not in _RECORD_VERDICTS:
        return "error", f"outcome record value {value!r} outside the closed set"
    recorded = cast(Verdict, value)
    if (recorded == "pass") != (junit_verdict == "pass"):
        return "error", f"outcome record {recorded} disagrees with junit {junit_verdict}"
    return recorded, f"outcome record: {recorded}"


def run_case(
    case: ir.Case,
    run_req: RunRequest,
    artifact_bind: node_artifact.NodeArtifact,
    work_root: Path,
    harness_bind: harness.HarnessBinding | None = None,
    outcome_source: str = "junit",
) -> CaseOutcome:
    """Execute one case in a fresh temp dir and a fresh interpreter process."""
    case_dir = Path(tempfile.mkdtemp(prefix="case-", dir=work_root))
    try:
        artifact = case_dir / "artifact"
        suite = case_dir / "suite"
        shutil.copytree(run_req.artifact_dir, artifact)
        shutil.copytree(run_req.suite_dir, suite)
        python_path = node_artifact.python_path(artifact_bind, artifact)
        if harness_bind is not None and run_req.harness is not None:
            harness_product = case_dir / "harness"
            harness_import_root = harness.lay_out(harness_bind, run_req.harness, harness_product)
            if harness.copy_digest(harness_bind, harness_product) != harness_bind.digest:
                return CaseOutcome(
                    case.case_id, "error", "harness bytes changed after binding", None, None
                )
            python_path = os.pathsep.join([python_path, str(harness_import_root)])
        ini = case_dir / "pytest.ini"
        ini.write_text("[pytest]\n", encoding="utf-8")
        junit = case_dir / "junit.xml"
        outcomes = case_dir / "outcomes.json"
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(case_dir),
            "PYTHONPATH": python_path,
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            # Only built-in pytest plugins: no entry-point plugin from the host
            # interpreter may alter collection, execution, or the JUnit report.
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "L9_NODE_ARTIFACT_DIR": str(artifact),
            # pytest/v2 suites write their per-case outcome record here.
            "L9_CONFORMANCE_OUTCOMES": str(outcomes),
        }
        if harness_bind is not None:
            # The rendered conftest reads L9_CONFORMANCE_DRIVER (module:factory).
            env["L9_CONFORMANCE_DRIVER"] = harness_bind.driver_ref
            env["L9_HARNESS_DRIVER"] = harness_bind.driver_ref
        interpreter = _interpreter(run_req.python_executable)
        cmd: Sequence[str] = (
            interpreter,
            "-B",
            "-m",
            "pytest",
            "-c",
            str(ini),
            "--rootdir",
            str(suite),
            "-p",
            "no:cacheprovider",
            "-q",
            f"--junitxml={junit}",
            str(suite / ir.checked_nodeid(case.nodeid)),
        )
        try:
            proc = subprocess.run(
                cmd,
                cwd=suite,
                env=env,
                capture_output=True,
                timeout=run_req.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CaseOutcome(case.case_id, "error", "timeout", None, None)
        if not junit.is_file():
            return CaseOutcome(
                case.case_id, "error", "no junit report produced", proc.returncode, None
            )
        verdict, detail = _classify_junit(junit, proc.returncode)
        if outcome_source == "outcome_record":
            verdict, detail = _classify_outcome_record(outcomes, case.case_id, verdict)
        return CaseOutcome(case.case_id, verdict, detail, proc.returncode, sha256_file(junit))
    finally:
        shutil.rmtree(case_dir, ignore_errors=True)


def _producer_digest() -> str:
    files = [
        {"path": p.relative_to(REPO_ROOT).as_posix(), "sha256": sha256_file(p)}
        for p in sorted(PACKAGE_DIR.glob("*.py"))
    ]
    files.append(
        {"path": SCHEMA_FILE.relative_to(REPO_ROOT).as_posix(), "sha256": sha256_file(SCHEMA_FILE)}
    )
    return file_set_digest(files)


def grade(conformance_ir: ir.ConformanceIR, unpublished_profiles: tuple[str, ...]) -> str:
    """Evidence grade from what was bound; isolation level never gates it (D3)."""
    if conformance_ir.synthetic:
        return "synthetic_test"
    if conformance_ir.canonical_admitted and not unpublished_profiles:
        return "canonical"
    return "candidate_authority_rehearsal"


def run(request: RunRequest, *, now: datetime | None = None) -> dict[str, Any]:
    verified = verify_inputs(request)
    cir = verified.conformance_ir
    obligations_out: list[dict[str, Any]] = []
    case_records: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="l9-conformance-run-") as work:
        work_root = Path(work)
        for obligation in cir.obligations:
            cases_out: list[dict[str, Any]] = []
            reason: str | None = None
            if obligation.disposition == "unresolved":
                verdict: Verdict = "unresolved"
                reason = f"IR disposition unresolved ({obligation.unresolved_reason})"
            elif obligation.verification_class != "fixture_replay":
                verdict = "unresolved"
                reason = (
                    f"verification class {obligation.verification_class} is not executed by this "
                    "runner and no external evidence is bound"
                )
            else:
                for case in obligation.cases:
                    outcome = run_case(
                        case,
                        request,
                        verified.artifact,
                        work_root,
                        verified.harness,
                        cir.outcome_source,
                    )
                    case_entry: dict[str, Any] = {
                        "case_id": outcome.case_id,
                        "verdict": outcome.verdict,
                        "detail": outcome.detail,
                    }
                    if outcome.junit_digest is not None:
                        case_entry["record_digest"] = outcome.junit_digest
                    cases_out.append(case_entry)
                    case_records.append(
                        {
                            "case_id": outcome.case_id,
                            "exit_code": outcome.exit_code,
                            "junit_digest": outcome.junit_digest,
                        }
                    )
                verdict = aggregate(cast(Verdict, c["verdict"]) for c in cases_out)
                if not obligation.cases:
                    reason = "obligation has no witness cases"
            entry: dict[str, Any] = {
                "obligation_id": obligation.obligation_id,
                "verification_class": obligation.verification_class,
                "required": obligation.required,
                "verdict": verdict,
                "cases": cases_out,
            }
            if reason is not None:
                entry["reason"] = reason
            obligations_out.append(entry)

    suite_files = [{"path": f.path, "sha256": f.sha256} for f in cir.suite_files]
    emitted = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body: dict[str, Any] = {
        "schema": SCHEMA_ID,
        "receipt_type": RECEIPT_TYPE,
        "exact_subject_ref": {
            "product_ref": cir.product_ref,
            "node_artifact": {
                "manifest_schema": verified.artifact.manifest_schema,
                "manifest_digest": verified.artifact.manifest_digest,
                "files": verified.artifact.files,
                **(
                    {"compilation_receipt": verified.artifact.compilation_receipt}
                    if verified.artifact.compilation_receipt is not None
                    else {}
                ),
            },
            "implementation": {
                "source_revision": request.source_revision,
                "source_revision_basis": "caller_supplied",
                "bound_source_digest": verified.bound_source_digest,
            },
        },
        "claim": CLAIM,
        "governing_coordinates": {
            "canonical_authority": {
                "repository": cir.canonical_repository,
                "revision": cir.canonical_revision,
                "admitted": cir.canonical_admitted,
            },
            "semantic_source_digests": [
                {"ref": s.ref, "digest": s.digest} for s in verified.semantic_sources
            ],
            "conformance_ir": {
                "ref": cir.ir_ref,
                "digest": request.ir_digest,
                **({"semantic_digest": cir.semantic_digest} if cir.semantic_digest else {}),
            },
            "fixture_ir": {
                "ref": cir.fixture_ir.ref,
                "digest": verified.fixture_raw_digest,
                **(
                    {"semantic_digest": verified.fixture_semantic_digest}
                    if verified.fixture_semantic_digest
                    else {}
                ),
            },
            "rendered_suite": {"digest": file_set_digest(suite_files), "files": suite_files},
            "profiles": [{"ref": p.ref, "digest": p.digest} for p in verified.profiles],
            **(
                {"harness": harness.coordinates(verified.harness)}
                if verified.harness is not None
                else {}
            ),
        },
        "observation_or_outcome": {
            "obligations": obligations_out,
            "overall_verdict": overall_from_obligations(obligations_out),
        },
        "verification_basis": {
            "isolation": {
                "requested": cir.isolation,
                "achieved": ISOLATION_ACHIEVED,
                "level": ISOLATION_LEVEL,
                "network": ISOLATION_NETWORK,
                "limitations": list(ISOLATION_LIMITATIONS),
            },
            "case_records": case_records,
            "reproduction": request.reproduction,
        },
        "producer_identity": {
            "runner": RUNNER_ID,
            "version": RUNNER_VERSION,
            "code_digest": _producer_digest(),
        },
        "evidence_grade": grade(cir, verified.unpublished_profiles),
        "authority": dict(AUTHORITY),
        "emitted_at": emitted,
    }
    return finalize(body)
