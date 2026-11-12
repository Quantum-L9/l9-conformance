#!/usr/bin/env python3
"""Run a bound conformance closure against a node artifact and emit a receipt.

Exit codes: 0 receipt emitted with overall verdict pass; 1 receipt emitted with a
non-pass overall verdict; 2 refused (an input could not be verified; no receipt).
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from l9_conformance.harness import HarnessRequest
from l9_conformance.runner import RunnerRefusal, RunRequest, run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ir", type=Path, required=True)
    parser.add_argument("--ir-digest", required=True)
    parser.add_argument("--fixture-ir", type=Path, required=True)
    parser.add_argument("--suite-dir", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--artifact-manifest", type=Path, required=True)
    parser.add_argument("--artifact-manifest-digest", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument(
        "--bound-source-digest",
        default=None,
        help="required for synthetic manifests; derived (and checked if given) for compiler nodes",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--harness-dir", type=Path, default=None, help="local harness root dir")
    parser.add_argument("--harness-root", default=None, help="declared repo-relative root")
    parser.add_argument("--harness-revision", default=None, help="harness source revision")
    parser.add_argument("--harness-driver", default=None, help="package.module:callable")
    args = parser.parse_args(argv)
    harness_args = (args.harness_dir, args.harness_root, args.harness_revision, args.harness_driver)
    if any(a is not None for a in harness_args) and not all(a is not None for a in harness_args):
        print("REFUSED: --harness-* options must be given together", file=sys.stderr)
        return 2
    harness = (
        HarnessRequest(
            root_dir=args.harness_dir,
            root=args.harness_root,
            source_revision=args.harness_revision,
            driver_ref=args.harness_driver,
        )
        if args.harness_dir is not None
        else None
    )
    request = RunRequest(
        ir_path=args.ir,
        ir_digest=args.ir_digest,
        fixture_ir_path=args.fixture_ir,
        suite_dir=args.suite_dir,
        artifact_dir=args.artifact_dir,
        artifact_manifest_path=args.artifact_manifest,
        artifact_manifest_digest=args.artifact_manifest_digest,
        source_revision=args.source_revision,
        bound_source_digest=args.bound_source_digest,
        timeout_seconds=args.timeout,
        harness=harness,
        reproduction="python3 scripts/run_conformance.py "
        + shlex.join(argv if argv is not None else sys.argv[1:]),
    )
    try:
        receipt = run(request)
    except RunnerRefusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    args.out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    verdict = receipt["observation_or_outcome"]["overall_verdict"]
    print(
        f"{receipt['receipt_identity']} overall_verdict={verdict} grade={receipt['evidence_grade']}"
    )
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
