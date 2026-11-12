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
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import yaml

from l9_conformance import RUNNER_ID, RUNNER_VERSION, ir
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

ISOLATION_ACHIEVED: Final = "fresh_tempdir_fresh_process_scrubbed_env"
ISOLATION_LIMITATIONS: Final = (
    "process-level isolation only: no container, no network namespace, no filesystem sandbox",
    "per D3 (pending owner decision) receipts under this isolation are capped at "
    "candidate_authority_rehearsal",
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
    bound_source_digest: str
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
    artifact_files: list[dict[str, str]] = field(default_factory=lambda: [])


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


def _load_catalog(index: Path) -> dict[str, str]:
    data = cast(Mapping[str, Any], yaml.safe_load(index.read_text(encoding="utf-8")))
    profiles = cast(list[Mapping[str, Any]], data.get("profiles") or [])
    return {str(p["id"]): str(p["digest"]) for p in profiles}


def _artifact_manifest(path: Path) -> list[dict[str, str]]:
    data = yaml.safe_load(path.read_bytes())
    if not isinstance(data, Mapping):
        raise RunnerRefusal("artifact manifest: expected mapping")
    files = cast(Mapping[str, Any], data).get("files")
    if not isinstance(files, list) or not files:
        raise RunnerRefusal("artifact manifest: files must be a non-empty list")
    out: list[dict[str, str]] = []
    for i, raw in enumerate(cast(list[Any], files)):
        if not isinstance(raw, Mapping):
            raise RunnerRefusal(f"artifact manifest: files[{i}] expected mapping")
        entry = cast(Mapping[str, Any], raw)
        rel, digest = entry.get("path"), entry.get("sha256")
        if not isinstance(rel, str) or not isinstance(digest, str):
            raise RunnerRefusal(f"artifact manifest: files[{i}] needs string path and sha256")
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise RunnerRefusal(
                f"artifact manifest: files[{i}].path must be relative and contained"
            )
        out.append({"path": rel, "sha256": digest})
    return out


def verify_inputs(request: RunRequest) -> _Verified:
    """Steps 1-5. Raises RunnerRefusal on any unverifiable binding."""
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
    for profile in conformance_ir.profiles:
        if profile.ref not in catalog:
            raise RunnerRefusal(
                f"profile {profile.ref} does not resolve in the l9-conformance catalog"
            )
        if catalog[profile.ref] != profile.digest:
            raise RunnerRefusal(
                f"profile {profile.ref} digest mismatch: bound {profile.digest}, "
                f"catalog {catalog[profile.ref]}"
            )

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

    if not request.artifact_manifest_path.is_file():
        raise RunnerRefusal(f"artifact manifest {request.artifact_manifest_path} is missing")
    manifest_digest = sha256_file(request.artifact_manifest_path)
    if manifest_digest != request.artifact_manifest_digest:
        raise RunnerRefusal(
            f"artifact manifest digest mismatch: bound {request.artifact_manifest_digest}, "
            f"actual {manifest_digest}"
        )
    artifact_files = _artifact_manifest(request.artifact_manifest_path)
    _check_exact_tree(
        request.artifact_dir, {f["path"]: f["sha256"] for f in artifact_files}, "node artifact"
    )
    return _Verified(conformance_ir=conformance_ir, artifact_files=artifact_files)


def _classify_junit(junit: Path, exit_code: int) -> tuple[Verdict, str]:
    try:
        root = ET.parse(junit).getroot()
    except (ET.ParseError, OSError) as exc:
        return "error", f"junit report unreadable: {exc}"
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


def run_case(case: ir.Case, request: RunRequest, work_root: Path) -> CaseOutcome:
    """Execute one case in a fresh temp dir and a fresh interpreter process."""
    case_dir = Path(tempfile.mkdtemp(prefix="case-", dir=work_root))
    try:
        artifact = case_dir / "artifact"
        suite = case_dir / "suite"
        shutil.copytree(request.artifact_dir, artifact)
        shutil.copytree(request.suite_dir, suite)
        ini = case_dir / "pytest.ini"
        ini.write_text("[pytest]\n", encoding="utf-8")
        junit = case_dir / "junit.xml"
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(case_dir),
            "PYTHONPATH": str(artifact),
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "L9_NODE_ARTIFACT_DIR": str(artifact),
        }
        cmd: Sequence[str] = (
            request.python_executable,
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
            str(suite / case.nodeid),
        )
        try:
            proc = subprocess.run(
                cmd,
                cwd=case_dir,
                env=env,
                capture_output=True,
                timeout=request.timeout_seconds,
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


def _grade(conformance_ir: ir.ConformanceIR) -> str:
    if conformance_ir.synthetic:
        return "synthetic_test"
    # Canonical grade requires admitted authority AND an isolation level not yet
    # decided (D3); process-level isolation caps the grade at rehearsal.
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
                    outcome = run_case(case, request, work_root)
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
                "manifest_digest": request.artifact_manifest_digest,
                "files": verified.artifact_files,
            },
            "implementation": {
                "source_revision": request.source_revision,
                "bound_source_digest": request.bound_source_digest,
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
        "evidence_grade": _grade(cir),
        "authority": dict(AUTHORITY),
        "emitted_at": emitted,
    }
    return finalize(body)
