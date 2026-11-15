#!/usr/bin/env python3
"""Regenerate (or check) the compiled closure test fixture from a semantic compiler checkout.

The fixture under ``testdata/compiled-closures/<name>/`` is real compiler output, never
hand-written: ``closure/`` is exactly ``<out>/conformance/`` of
``l9-semantic-compiler birth-closure`` and ``product/`` holds only the product's declared
conformance harness root (the driver the closure binds by digest).

The product is the compiler's own A10 State specimen (``tests/specimens/l9-state-711da68``),
prepared exactly as the compiler's ``tests/test_birth_closure.py::_state`` prepares it (the
modelled Gate registration coordinate and the declared conformance driver). Run with an
interpreter where the compiler checkout is installed::

    python scripts/regen_compiled_closure.py --compiler-src <l9-semantic-compiler-engine> [--check]

``--check`` regenerates into a temporary directory and fails on any byte difference.
"""

from __future__ import annotations

import argparse
import filecmp
import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "testdata" / "compiled-closures" / "l9-state-711da68"
PROVENANCE = "PROVENANCE.md"


def _generate(compiler_src: Path, dest: Path) -> None:
    sys.path.insert(0, str(compiler_src / "tests"))
    cwd = Path.cwd()
    os.chdir(compiler_src)  # the compiler helper reads repository-relative paths
    try:
        helper: Any = importlib.import_module("test_birth_closure")
        birth: Any = importlib.import_module("l9_semantic_compiler.product.birth_closure")
        with tempfile.TemporaryDirectory() as tmp:
            args = helper._state(Path(tmp) / "product")
            build = birth.compile_birth_closure(source_revision=helper.REVISION, **args)
            product_root = Path(args["repository_path"]).parent
            out = Path(tmp) / "out"
            birth.write_birth_closure(build, out, product_root=product_root)
            shutil.copytree(out / "conformance", dest / "closure")
            factory = build.files["closure.json"]
            import json

            harness_root = json.loads(factory)["driver_factory"]["harness_root"]
            shutil.copytree(
                product_root / harness_root,
                dest / "product" / harness_root,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
    finally:
        os.chdir(cwd)


def _same(a: Path, b: Path) -> bool:
    cmp = filecmp.dircmp(a, b, ignore=[PROVENANCE])
    if cmp.left_only or cmp.right_only or cmp.funny_files:
        return False
    _, mismatch, errors = filecmp.cmpfiles(a, b, cmp.common_files, shallow=False)
    if mismatch or errors:
        return False
    return all(_same(a / d, b / d) for d in cmp.common_dirs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler-src", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "fixture"
        _generate(args.compiler_src.resolve(), fresh)
        if args.check:
            if not _same(fresh, FIXTURE):
                print(f"DIFFERS: {FIXTURE} is not the compiler's output", file=sys.stderr)
                return 1
            print(f"OK: {FIXTURE} matches the compiler's output")
            return 0
        provenance = FIXTURE / PROVENANCE
        kept = provenance.read_bytes() if provenance.is_file() else None
        shutil.rmtree(FIXTURE, ignore_errors=True)
        shutil.copytree(fresh, FIXTURE)
        if kept is not None:
            provenance.write_bytes(kept)
    print(f"wrote {FIXTURE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
