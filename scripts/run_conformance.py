#!/usr/bin/env python3
"""Run a compiler conformance closure against its node artifact and emit a receipt.

The script takes no path arguments. Every input and the output live under one fixed work
directory, ``work/`` at the repository root (git-ignored), under fixed names:

    work/closure/closure.json   the compiler's ``<out>/conformance/`` directory
                                (``l9.conformance-closure/v1``, l9-semantic-compiler
                                ``birth-closure``), copied or linked to ``work/closure``
    work/product/               the product checkout holding the conformance driver
                                harness (bound per ruling D5; the driver module must match
                                the closure's ``driver_factory.digest``)
    work/receipt.json           written: the ``l9-conformance/conformance-receipt@1`` receipt

Any receipt left from an earlier run is removed first, so a refusal leaves none. The only
option is ``--timeout`` (seconds per case). Other input forms (synthetic IR, a v1 IR with a
suite binding) are the Python API (:class:`l9_conformance.runner.RunRequest`) and
``scripts/rehearse.py``.

Exit codes: 0 receipt emitted with overall verdict pass; 1 receipt emitted with a
non-pass overall verdict; 2 refused (an input could not be verified; no receipt).
The birth gate is the receipt's ``observation_or_outcome.birth_gate.birth_passable``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from l9_conformance.runner import ClosureRunRequest, RunnerRefusal, run_closure

WORK_DIR: Final = Path(__file__).resolve().parents[1] / "work"
CLOSURE_FILE: Final = "closure/closure.json"
PRODUCT_DIR: Final = "product"
RECEIPT_FILE: Final = "receipt.json"
REPRODUCTION: Final = (
    "python3 scripts/run_conformance.py  # work/closure/closure.json, work/product"
)
MAX_TIMEOUT_SECONDS: Final = 86400.0


def _timeout(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from exc
    if not 0 < seconds <= MAX_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError(f"timeout must be in (0, {MAX_TIMEOUT_SECONDS:g}]")
    return seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--timeout", type=_timeout, default=120.0, help="seconds per case")
    args = parser.parse_args(argv)
    work = WORK_DIR
    receipt_path = work / RECEIPT_FILE
    receipt_path.unlink(missing_ok=True)
    closure_path = work / CLOSURE_FILE
    if not closure_path.is_file():
        print(f"REFUSED: no closure at {closure_path}", file=sys.stderr)
        return 2
    try:
        receipt = run_closure(
            ClosureRunRequest(
                closure_path=closure_path,
                product_root=work / PRODUCT_DIR,
                timeout_seconds=args.timeout,
                reproduction=REPRODUCTION,
            )
        )
    except RunnerRefusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    outcome = receipt["observation_or_outcome"]
    verdict = outcome["overall_verdict"]
    print(
        f"{receipt['receipt_identity']} overall_verdict={verdict} grade={receipt['evidence_grade']}"
        f" birth_passable={outcome['birth_gate']['birth_passable']}"
    )
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
