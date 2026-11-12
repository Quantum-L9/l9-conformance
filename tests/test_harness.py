"""Conformance-harness binding (ruling D5). Harness trees here are SYNTHETIC."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import Closure, ObligationSpec, replay

from l9_conformance.digests import sha256_file
from l9_conformance.harness import HarnessRefused, HarnessRequest, bind
from l9_conformance.receipt import verify_receipt
from l9_conformance.runner import RunnerRefusal, run

Build = Callable[[list[ObligationSpec]], Closure]
ROOT = "src/toy_harness"
DRIVER = "toy_harness.driver:create_driver"


def _harness(tmp_path: Path) -> Path:
    pkg = tmp_path / "wip" / "src" / "toy_harness"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "driver.py").write_text(
        "from toy_harness.observations import observe\n\n"
        "def create_driver():\n    return observe\n",
        encoding="utf-8",
    )
    (pkg / "observations.py").write_text("def observe():\n    return 42\n", encoding="utf-8")
    return pkg


def _req(pkg: Path, **kw: str) -> HarnessRequest:
    args = {"root": ROOT, "source_revision": "wip@" + "a" * 40, "driver_ref": DRIVER} | kw
    return HarnessRequest(root_dir=pkg, **args)


def test_binds_every_file_under_root(tmp_path: Path) -> None:
    binding = bind(_req(_harness(tmp_path)))
    assert {f["path"] for f in binding.files} == {
        f"{ROOT}/__init__.py",
        f"{ROOT}/driver.py",
        f"{ROOT}/observations.py",
    }
    assert binding.digest.startswith("sha256:")


def _noop(p: Path) -> None:
    return None


def _pycache(p: Path) -> None:
    (p / "__pycache__").mkdir()


def _pyc(p: Path) -> None:
    (p / "x.pyc").write_bytes(b"\0")


def _symlink(p: Path) -> None:
    (p / "link.py").symlink_to(p / "driver.py")


REFUSALS: list[tuple[Callable[[Path], None], dict[str, str], str]] = [
    (_pycache, {}, "bytecode directory"),
    (_pyc, {}, "bytecode file"),
    (_symlink, {}, "symlink"),
    (_noop, {"driver_ref": "other_pkg.driver:create_driver"}, "not inside"),
    (_noop, {"driver_ref": "toy_harness.missing:create_driver"}, "no bound source"),
    (_noop, {"driver_ref": "toy_harness.driver"}, "module:callable"),
    (_noop, {"root": "../toy_harness"}, "relative and contained"),
    (_noop, {"root": "src/other_name"}, "does not match"),
]


@pytest.mark.parametrize(("mutate", "kwargs", "match"), REFUSALS)
def test_harness_refusals(
    tmp_path: Path,
    mutate: Callable[[Path], None],
    kwargs: dict[str, str],
    match: str,
) -> None:
    pkg = _harness(tmp_path)
    mutate(pkg)
    with pytest.raises(HarnessRefused, match=match):
        bind(_req(pkg, **kwargs))


def test_run_binds_harness_and_cases_import_it(make_closure: Build, tmp_path: Path) -> None:
    pkg = _harness(tmp_path)
    closure = make_closure([])
    suite = closure.root / "suite" / "test_toy.py"
    suite.write_text(
        "import importlib, os\n\n"
        "def test_driver():\n"
        "    mod, fn = os.environ['L9_HARNESS_DRIVER'].split(':')\n"
        "    assert getattr(importlib.import_module(mod), fn)()() == 42\n",
        encoding="utf-8",
    )
    closure.ir_doc["rendered_suite"]["files"][0]["sha256"] = sha256_file(suite)
    closure.ir_doc["obligations"] = [replay("ob/h", "test_driver")]
    receipt = run(closure.request(harness=_req(pkg)))
    verify_receipt(receipt)
    assert receipt["observation_or_outcome"]["overall_verdict"] == "pass"
    bound = receipt["governing_coordinates"]["harness"]
    assert bound["root"] == ROOT
    assert bound["driver_ref"] == DRIVER
    assert bound["source_revision_basis"] == "caller_supplied"
    assert len(bound["files"]) == 3


def test_run_refuses_unbindable_harness(make_closure: Build, tmp_path: Path) -> None:
    pkg = _harness(tmp_path)
    (pkg / "__pycache__").mkdir()
    closure = make_closure([replay("ob/a", "test_pass")])
    with pytest.raises(RunnerRefusal, match="bytecode directory"):
        run(closure.request(harness=_req(pkg)))


def test_receipt_without_harness_has_no_harness_block(make_closure: Build) -> None:
    receipt = run(make_closure([replay("ob/a", "test_pass")]).request())
    assert "harness" not in receipt["governing_coordinates"]
