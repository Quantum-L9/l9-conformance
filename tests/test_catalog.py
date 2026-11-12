"""Catalog integrity: real catalog passes; tampered copies fail."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from check_catalog import _check_law, check  # pyright: ignore[reportPrivateUsage]

ROOT = Path(__file__).resolve().parents[1]
AS_PROFILES = [
    f"l9.fixture-profile/authoritative-state-{c}@1"
    for c in ("core", "capability", "runtime", "adapter")
]


@pytest.fixture
def copy_root(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "catalog", tmp_path / "catalog")
    shutil.copy(ROOT / "SHA256SUMS", tmp_path / "SHA256SUMS")
    return tmp_path


def _rebind(root: Path, rel: str) -> None:
    """Re-record a changed file's digest in INDEX and SHA256SUMS (simulates a careless edit)."""
    new = hashlib.sha256((root / rel).read_bytes()).hexdigest()
    index = root / "catalog" / "INDEX.yaml"
    sums = root / "SHA256SUMS"
    old = next(
        line.split()[0] for line in sums.read_text().splitlines() if line.endswith("  " + rel)
    )
    index.write_text(index.read_text().replace(old, new))
    sums.write_text(sums.read_text().replace(old, new))
    for profile in (root / "catalog").glob("fixture-profile-*.yaml"):
        profile.write_text(profile.read_text().replace(old, new))


def test_real_catalog_passes() -> None:
    assert check(ROOT) == []


def test_catalog_lists_four_authoritative_state_profiles() -> None:
    index: dict[str, Any] = yaml.safe_load((ROOT / "catalog" / "INDEX.yaml").read_text())
    entries = {p["id"]: p for p in index["profiles"]}
    for pid in AS_PROFILES:
        assert entries[pid]["status"] == "candidate"
        assert len(entries[pid]["membership"]) == 1
    assert entries["l9.fixture-profile/core@1"]["status"] == "published"


def test_profiles_record_unknowns_not_law() -> None:
    for cls in ("core", "capability", "runtime", "adapter"):
        profile: dict[str, Any] = yaml.safe_load(
            (ROOT / "catalog" / f"fixture-profile-authoritative-state-{cls}-1.yaml").read_text()
        )
        kinds = {u["kind"] for u in profile["unresolved"]}
        assert kinds == {"obligation_content", "class_assignment", "product_fixture_sets"}
        assert profile["publication"]["admitted"] is False


def test_tampered_profile_bytes_fail(copy_root: Path) -> None:
    path = copy_root / "catalog" / "fixture-profile-authoritative-state-core-1.yaml"
    path.write_text(path.read_text() + "# edit\n")
    assert any("digest" in f for f in check(copy_root))


def test_orphan_sha256sums_line_fails(copy_root: Path) -> None:
    sums = copy_root / "SHA256SUMS"
    sums.write_text(sums.read_text() + "0" * 64 + "  catalog/ghost.yaml\n")
    assert any("no INDEX entry binds" in f for f in check(copy_root))


def test_missing_sha256sums_line_fails(copy_root: Path) -> None:
    sums = copy_root / "SHA256SUMS"
    sums.write_text(
        "\n".join(line for line in sums.read_text().splitlines() if "runtime-1.yaml" not in line)
        + "\n"
    )
    assert any("SHA256SUMS entry None" in f for f in check(copy_root))


def test_fixture_class_drift_fails_even_when_rebound(copy_root: Path) -> None:
    rel = "catalog/fixtures/authoritative-state-requires-adapter.yaml"
    path = copy_root / rel
    path.write_text(
        path.read_text().replace("conformance_class: adapter", "conformance_class: core")
    )
    _rebind(copy_root, rel)
    # Profile bytes changed too (membership_digests), so rebind the profile as well.
    _rebind(copy_root, "catalog/fixture-profile-authoritative-state-adapter-1.yaml")
    failures = check(copy_root)
    assert any("differs from profile" in f for f in failures)


def _law(tmp_path: Path, classes: list[str]) -> tuple[Path, dict[str, Any]]:
    law = tmp_path / "law"
    (law / "semantics").mkdir(parents=True)
    doc = {"archetypes": [{"id": "x/arch@1", "requires": {"conformance_classes": classes}}]}
    path = law / "semantics" / "node_archetypes.yaml"
    path.write_text(yaml.safe_dump(doc))
    fixture: dict[str, Any] = {
        "id": "x/fixture@1",
        "conformance_class": "adapter",
        "source_law": {
            "path": "semantics/node_archetypes.yaml",
            "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        },
        "observable": {"archetype_id": "x/arch@1", "required_conformance_classes": classes},
    }
    return law, fixture


def test_law_trace_positive(tmp_path: Path) -> None:
    law, fixture = _law(tmp_path, ["core", "adapter"])
    failures: list[str] = []
    _check_law(fixture, law, failures)
    assert failures == []


def test_law_trace_refuses_invented_class(tmp_path: Path) -> None:
    law, fixture = _law(tmp_path, ["core"])
    fixture["observable"]["required_conformance_classes"] = ["core", "adapter"]
    failures: list[str] = []
    _check_law(fixture, law, failures)
    assert any("does not require class adapter" in f for f in failures)


def test_law_trace_refuses_changed_law_bytes(tmp_path: Path) -> None:
    law, fixture = _law(tmp_path, ["core", "adapter"])
    (law / "semantics" / "node_archetypes.yaml").write_text("archetypes: []\n")
    failures: list[str] = []
    _check_law(fixture, law, failures)
    assert any("differs from pinned" in f for f in failures)
