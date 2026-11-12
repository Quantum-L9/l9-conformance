#!/usr/bin/env python3
"""Rehearsal driver: fetch pinned commits, verify them, bind every input, run the runner.

This script never executes compiler code. The compiler's outputs (conformance IR, fixture
IR, rendered pytest/v2 suite, fresh node output directory) are supplied as DATA paths in
the plan; the runner verifies each against its bound digests before any case executes.

Plan file (YAML), ``l9-conformance/rehearsal-plan@0``::

    schema: l9-conformance/rehearsal-plan@0
    workdir: <directory for pinned checkouts>
    repos:                       # each fetched at exactly this commit, depth 1
      authority: {url: https://github.com/Quantum-L9/.github, commit: <40-hex>}
      subject:   {url: https://github.com/Quantum-L9/l9-wip,  commit: <40-hex>}
    semantic_sources:            # files in the authority checkout, bound by raw sha256
      - semantics/conformance_ir.schema.yaml
    harness:                     # inside the subject checkout
      declared_in: <subject-relative path of the product's repository-spec.yaml>
      root: <optional; must be one of its conformance_harness_roots>
      driver_ref: <package.module:callable>
    compiler_outputs:            # data paths produced by the compiler (not run here)
      ir: <path>
      ir_digest: sha256:<raw>
      ir_semantic_digest: sha256:<semantic>
      fixture_ir: <path>
      suite_dir: <path>
      suite_binding: <path>
      suite_binding_digest: sha256:<raw>
      node_dir: <path>
      node_manifest_digest: sha256:<semantic>
    python: <interpreter of the rehearsal venv>
    out: <receipt output path>

The harness root is never assumed: it is read from
``repository_structure.conformance_harness_roots`` in ``declared_in`` and resolved
relative to that file's directory; the declaring file is bound as a semantic source.

Exit codes follow scripts/run_conformance.py (0 pass, 1 non-pass receipt, 2 refused).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, cast

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from l9_conformance.harness import HarnessRequest
from l9_conformance.runner import RunnerRefusal, RunRequest, run

PLAN_SCHEMA = "l9-conformance/rehearsal-plan@0"


class PlanError(RuntimeError):
    """The plan or a pinned checkout could not be verified."""


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=900, check=False
    )
    if proc.returncode != 0:
        raise PlanError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def fetch_pinned(url: str, commit: str, dest: Path) -> Path:
    """Fetch exactly ``commit`` (depth 1) into ``dest`` and check it out detached."""
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise PlanError(f"commit {commit!r} is not a 40-hex sha")
    dest.mkdir(parents=True, exist_ok=True)
    if not (dest / ".git").exists():
        _git("init", "-q", cwd=dest)
        _git("remote", "add", "origin", url, cwd=dest)
    elif _git("remote", "get-url", "origin", cwd=dest).lower() != url.lower():
        raise PlanError(f"{dest} has a different origin than {url}")
    _git("fetch", "-q", "--depth", "1", "origin", commit, cwd=dest)
    _git("checkout", "-q", "--detach", commit, cwd=dest)
    head = _git("rev-parse", "HEAD", cwd=dest)
    if head != commit:
        raise PlanError(f"{dest} HEAD {head} is not the pinned {commit}")
    if _git("status", "--porcelain", "--untracked-files=all", cwd=dest):
        raise PlanError(f"{dest} is not a clean checkout of {commit}")
    return dest


def _m(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlanError(f"{where}: expected mapping")
    return cast(Mapping[str, Any], value)


def declared_harness_root(subject_dir: Path, declared_in: str, requested: object) -> str:
    """The harness root exactly as the product declares it, never assumed.

    Reads ``repository_structure.conformance_harness_roots`` from ``declared_in`` (a
    subject-relative repository-spec). A single declared root is used as-is; with
    several, ``requested`` must name one of them. Returned without a trailing slash.
    """
    rel = PurePosixPath(declared_in)
    if not declared_in or rel.is_absolute() or ".." in rel.parts:
        raise PlanError(f"harness.declared_in {declared_in!r} must be relative and contained")
    path = subject_dir / declared_in
    if not path.is_file() or path.is_symlink():
        raise PlanError(f"harness.declared_in {declared_in!r} is not a regular file")
    try:
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PlanError(f"{declared_in}: unparseable: {exc}") from exc
    structure = _m(_m(spec, declared_in).get("repository_structure"), "repository_structure")
    raw = structure.get("conformance_harness_roots")
    if not isinstance(raw, list) or not raw:
        raise PlanError(f"{declared_in} declares no repository_structure.conformance_harness_roots")
    roots: list[str] = []
    for item in cast(list[Any], raw):
        if not isinstance(item, str) or not item.strip("/"):
            raise PlanError(f"{declared_in}: conformance_harness_roots entry {item!r} is invalid")
        roots.append(item.rstrip("/"))
    if requested is None:
        if len(roots) != 1:
            raise PlanError(
                f"{declared_in} declares {len(roots)} harness roots; plan must name one"
            )
        return roots[0]
    wanted = str(requested).rstrip("/")
    if wanted not in roots:
        raise PlanError(f"harness.root {requested!r} is not declared in {declared_in}: {roots}")
    return wanted


def build_request(plan: Mapping[str, Any]) -> RunRequest:
    if plan.get("schema") != PLAN_SCHEMA:
        raise PlanError(f"plan schema must be {PLAN_SCHEMA}")
    workdir = Path(str(plan["workdir"]))
    repos = _m(plan.get("repos"), "repos")
    authority = _m(repos.get("authority"), "repos.authority")
    subject = _m(repos.get("subject"), "repos.subject")
    auth_dir = fetch_pinned(str(authority["url"]), str(authority["commit"]), workdir / "authority")
    subj_dir = fetch_pinned(str(subject["url"]), str(subject["commit"]), workdir / "subject")
    auth_slug = str(authority["url"]).rstrip("/").split("github.com/")[-1]
    sources: list[tuple[str, Path]] = []
    for rel in cast(list[str], plan.get("semantic_sources") or []):
        sources.append((f"{auth_slug}@{authority['commit']}:{rel}", auth_dir / rel))
    harness_plan = _m(plan.get("harness"), "harness")
    subj_slug = str(subject["url"]).rstrip("/").split("github.com/")[-1]
    declared_in = str(harness_plan["declared_in"])
    root = declared_harness_root(subj_dir, declared_in, harness_plan.get("root"))
    sources.append((f"{subj_slug}@{subject['commit']}:{declared_in}", subj_dir / declared_in))
    harness = HarnessRequest(
        root_dir=subj_dir / PurePosixPath(declared_in).parent / root,
        root=root,
        source_revision=f"{subj_slug}@{subject['commit']}",
        driver_ref=str(harness_plan["driver_ref"]),
    )
    out = _m(plan.get("compiler_outputs"), "compiler_outputs")
    node_dir = Path(str(out["node_dir"]))
    return RunRequest(
        ir_path=Path(str(out["ir"])),
        ir_digest=str(out["ir_digest"]),
        fixture_ir_path=Path(str(out["fixture_ir"])),
        suite_dir=Path(str(out["suite_dir"])),
        artifact_dir=node_dir,
        artifact_manifest_path=node_dir / "evidence" / "artifact-manifest.yaml",
        artifact_manifest_digest=str(out["node_manifest_digest"]),
        source_revision=f"{subj_slug}@{subject['commit']}",
        python_executable=str(plan["python"]),
        harness=harness,
        semantic_sources=tuple(sources),
        suite_binding_path=Path(str(out["suite_binding"])),
        suite_binding_digest=str(out["suite_binding_digest"]),
        ir_semantic_digest=str(out["ir_semantic_digest"]),
        reproduction="python3 scripts/rehearse.py --plan <plan>",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args(argv)
    plan = _m(yaml.safe_load(args.plan.read_text(encoding="utf-8")), "plan")
    try:
        request = build_request(plan)
        receipt = run(request)
    except (PlanError, RunnerRefusal) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    out = Path(str(plan["out"]))
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    verdict = receipt["observation_or_outcome"]["overall_verdict"]
    print(
        f"{receipt['receipt_identity']} overall_verdict={verdict} grade={receipt['evidence_grade']}"
    )
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
