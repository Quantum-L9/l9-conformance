#!/usr/bin/env python3
"""Run a bound conformance closure against a node artifact and emit a receipt.

Two input forms, never mixed:

* ``--closure <dir>/conformance/closure.json --product-root <product checkout>``: a compiler
  closure (``l9.conformance-closure/v1``, l9-semantic-compiler ``birth-closure``). Every
  binding comes from the closure; the product root supplies only the conformance driver
  harness (bound per ruling D5; the driver module must match the closure's digest).
* ``--ir ... --source-revision ...``: the individual bound inputs (synthetic IR, or a v1 IR
  with its digest-bound ``--suite-binding``).

Exit codes: 0 receipt emitted with overall verdict pass; 1 receipt emitted with a
non-pass overall verdict; 2 refused (an input could not be verified; no receipt).
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from l9_conformance.harness import HarnessRequest
from l9_conformance.runner import ClosureRunRequest, RunnerRefusal, RunRequest, run, run_closure

_INDIVIDUAL = (
    "ir",
    "ir_digest",
    "fixture_ir",
    "suite_dir",
    "artifact_dir",
    "artifact_manifest",
    "artifact_manifest_digest",
    "source_revision",
)
_FLAG_ONLY = (
    "bound_source_digest",
    "suite_binding",
    "suite_binding_digest",
    "ir_semantic_digest",
    "harness_dir",
    "harness_root",
    "harness_revision",
    "harness_driver",
)


def _flag(name: str) -> str:
    return "--" + name.replace("_", "-")


def _emit(receipt: dict[str, Any], out: Path) -> int:
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    outcome = receipt["observation_or_outcome"]
    verdict = outcome["overall_verdict"]
    print(
        f"{receipt['receipt_identity']} overall_verdict={verdict} grade={receipt['evidence_grade']}"
        f" birth_passable={outcome['birth_gate']['birth_passable']}"
    )
    return 0 if verdict == "pass" else 1


def _closure_main(args: argparse.Namespace, parser: argparse.ArgumentParser, repro: str) -> int:
    mixed = [n for n in (*_INDIVIDUAL[:-1], *_FLAG_ONLY) if getattr(args, n) is not None]
    if mixed or args.semantic_source:
        parser.error(
            "--closure takes its bindings from the closure; do not also pass "
            + ", ".join(_flag(n) for n in mixed or ["semantic_source"])
        )
    if args.product_root is None:
        parser.error("--closure requires --product-root (the conformance driver harness)")
    try:
        receipt = run_closure(
            ClosureRunRequest(
                closure_path=args.closure,
                product_root=args.product_root,
                source_revision=args.source_revision,
                timeout_seconds=args.timeout,
                reproduction=repro,
            )
        )
    except RunnerRefusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return _emit(receipt, args.out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--closure", type=Path, help="compiler closure.json (closure/v1)")
    parser.add_argument(
        "--product-root", type=Path, help="with --closure: product checkout holding the harness"
    )
    parser.add_argument("--ir", type=Path)
    parser.add_argument("--ir-digest")
    parser.add_argument("--fixture-ir", type=Path)
    parser.add_argument("--suite-dir", type=Path)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--artifact-manifest", type=Path)
    parser.add_argument("--artifact-manifest-digest")
    parser.add_argument("--source-revision")
    parser.add_argument(
        "--bound-source-digest",
        default=None,
        help="required for synthetic manifests; derived (and checked if given) for compiler nodes",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--suite-binding", type=Path, default=None, help="v1: suite binding file")
    parser.add_argument("--suite-binding-digest", default=None, help="v1: sha256 of the binding")
    parser.add_argument(
        "--ir-semantic-digest", default=None, help="v1: expected IR semantic digest"
    )
    parser.add_argument(
        "--semantic-source",
        action="append",
        default=[],
        metavar="REF=PATH",
        help="governing semantic source file to bind by raw sha256 (repeatable)",
    )
    parser.add_argument("--harness-dir", type=Path, default=None, help="local harness root dir")
    parser.add_argument("--harness-root", default=None, help="declared repo-relative root")
    parser.add_argument("--harness-revision", default=None, help="harness source revision")
    parser.add_argument("--harness-driver", default=None, help="package.module:callable")
    args = parser.parse_args(argv)
    repro = "python3 scripts/run_conformance.py " + shlex.join(
        argv if argv is not None else sys.argv[1:]
    )
    if args.closure is not None:
        return _closure_main(args, parser, repro)
    missing = [n for n in _INDIVIDUAL if getattr(args, n) is None]
    if missing:
        parser.error("missing " + ", ".join(_flag(n) for n in missing))
    if args.product_root is not None:
        parser.error("--product-root is only meaningful with --closure")
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
    sources: list[tuple[str, Path]] = []
    for item in args.semantic_source:
        ref, sep, path = item.partition("=")
        if not sep or not ref or not path:
            print(f"REFUSED: --semantic-source {item!r} is not REF=PATH", file=sys.stderr)
            return 2
        sources.append((ref, Path(path)))
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
        semantic_sources=tuple(sources),
        suite_binding_path=args.suite_binding,
        suite_binding_digest=args.suite_binding_digest,
        ir_semantic_digest=args.ir_semantic_digest,
        reproduction=repro,
    )
    try:
        receipt = run(request)
    except RunnerRefusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return _emit(receipt, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
