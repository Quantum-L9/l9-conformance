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

from l9_conformance import RUNNER_ID, RUNNER_VERSION, ir, node_artifact
from l9_conformance.digests import file_set_digest, sha256_bytes, sha256_file
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
    root = index.resolve().parents[1]
    return {
        str(p["id"]): (str(p["digest"]), str(p.get("status", "")), root / str(p["path"]))
        for p in profiles
    }


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
    try:
        conformance_ir = ir.load(ir_bytes)
    except ir.IRInvalid as exc:
        raise RunnerRefusal(f"conformance IR invalid: {exc}") from exc

    if not request.fixture_ir_path.is_file():
        raise RunnerRefusal(f"fixture IR {request.fixture_ir_path} is missing")
    fixture_digest = sha256_file(request.fixture_ir_path)
    if fixture_digest != conformance_ir.fixture_ir.digest:
        raise RunnerRefusal(
            f"fixture IR digest mismatch: bound {conformance_ir.fixture_ir.digest}, "
            f"actual {fixture_digest}"
        )

    catalog = _load_catalog(request.catalog_index)
    unpublished: list[str] = []
    for profile in conformance_ir.profiles:
        if profile.ref not in catalog:
            raise RunnerRefusal(
                f"profile {profile.ref} does not resolve in the l9-conformance catalog"
            )
        catalog_digest, catalog_status, profile_path = catalog[profile.ref]
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

    suite_paths = {f.path for f in conformance_ir.suite_files}
    _check_exact_tree(
        request.suite_dir, {f.path: f.sha256 for f in conformance_ir.suite_files}, "rendered suite"
    )
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
    return _Verified(
        conformance_ir=conformance_ir,
        artifact=artifact,
        bound_source_digest=bound_source,
        unpublished_profiles=tuple(unpublished),
    )


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


def run_case(
    case: ir.Case, run_req: RunRequest, artifact_bind: node_artifact.NodeArtifact, work_root: Path
) -> CaseOutcome:
    """Execute one case in a fresh temp dir and a fresh interpreter process."""
    case_dir = Path(tempfile.mkdtemp(prefix="case-", dir=work_root))
    try:
        artifact = case_dir / "artifact"
        suite = case_dir / "suite"
        shutil.copytree(run_req.artifact_dir, artifact)
        shutil.copytree(run_req.suite_dir, suite)
        ini = case_dir / "pytest.ini"
        ini.write_text("[pytest]\n", encoding="utf-8")
        junit = case_dir / "junit.xml"
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(case_dir),
            "PYTHONPATH": node_artifact.python_path(artifact_bind, artifact),
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            # Only built-in pytest plugins: no entry-point plugin from the host
            # interpreter may alter collection, execution, or the JUnit report.
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "L9_NODE_ARTIFACT_DIR": str(artifact),
        }
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
                cwd=case_dir,
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
            if obligation.verification_class != "fixture_replay":
                verdict: Verdict = "unresolved"
                reason = (
                    f"verification class {obligation.verification_class} is not executed by this "
                    "runner and no external evidence is bound"
                )
            else:
                for case in obligation.cases:
                    outcome = run_case(case, request, verified.artifact, work_root)
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
                {"ref": s.ref, "digest": s.digest} for s in cir.semantic_sources
            ],
            "conformance_ir": {"ref": cir.ir_ref, "digest": request.ir_digest},
            "fixture_ir": {"ref": cir.fixture_ir.ref, "digest": cir.fixture_ir.digest},
            "rendered_suite": {"digest": file_set_digest(suite_files), "files": suite_files},
            "profiles": [{"ref": p.ref, "digest": p.digest} for p in cir.profiles],
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
