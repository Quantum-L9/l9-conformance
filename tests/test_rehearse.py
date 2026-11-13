"""Rehearsal driver: pinned-commit fetching (local repositories only, no network)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from rehearse import PlanError, declared_harness_root, fetch_pinned, stage_source_subject


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> tuple[str, str]:
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "uploadpack.allowReachableSHA1InWant", "true")
    (repo / "f.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "one")
    return f"file://{repo}", _git(repo, "rev-parse", "HEAD")


def test_fetch_pinned_checks_out_exact_commit(origin: tuple[str, str], tmp_path: Path) -> None:
    url, commit = origin
    dest = fetch_pinned(url, commit, tmp_path / "pinned")
    assert _git(dest, "rev-parse", "HEAD") == commit
    assert (dest / "f.txt").read_text(encoding="utf-8") == "one\n"
    # Idempotent on an existing checkout of the same origin.
    assert fetch_pinned(url, commit, dest) == dest


def test_fetch_pinned_rejects_non_sha(origin: tuple[str, str], tmp_path: Path) -> None:
    url, _ = origin
    with pytest.raises(PlanError, match="40-hex"):
        fetch_pinned(url, "main", tmp_path / "pinned")


def test_fetch_pinned_rejects_unknown_commit(origin: tuple[str, str], tmp_path: Path) -> None:
    url, _ = origin
    with pytest.raises(PlanError, match="failed"):
        fetch_pinned(url, "0" * 40, tmp_path / "pinned")


def test_fetch_pinned_rejects_other_origin(origin: tuple[str, str], tmp_path: Path) -> None:
    url, commit = origin
    dest = fetch_pinned(url, commit, tmp_path / "pinned")
    with pytest.raises(PlanError, match="different origin"):
        fetch_pinned("file:///elsewhere", commit, dest)


def test_fetch_pinned_rejects_dirty_checkout(origin: tuple[str, str], tmp_path: Path) -> None:
    url, commit = origin
    dest = fetch_pinned(url, commit, tmp_path / "pinned")
    (dest / "f.txt").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(PlanError, match="not a clean checkout"):
        fetch_pinned(url, commit, dest)


def _spec(tmp_path: Path, body: str) -> Path:
    subject = tmp_path / "subject"
    (subject / "pkg").mkdir(parents=True)
    (subject / "pkg" / "repository-spec.yaml").write_text(body, encoding="utf-8")
    return subject


def test_declared_root_read_from_spec(tmp_path: Path) -> None:
    subject = _spec(
        tmp_path,
        "repository_structure:\n  conformance_harness_roots:\n  - src/h_harness/\n",
    )
    assert declared_harness_root(subject, "pkg/repository-spec.yaml", None) == "src/h_harness"
    assert (
        declared_harness_root(subject, "pkg/repository-spec.yaml", "src/h_harness/")
        == "src/h_harness"
    )


def test_undeclared_root_refused(tmp_path: Path) -> None:
    subject = _spec(tmp_path, "repository_structure:\n  implementation_roots:\n  - src/\n")
    with pytest.raises(PlanError, match="declares no"):
        declared_harness_root(subject, "pkg/repository-spec.yaml", None)


def test_root_not_in_declaration_refused(tmp_path: Path) -> None:
    subject = _spec(
        tmp_path,
        "repository_structure:\n  conformance_harness_roots:\n  - src/h_harness/\n",
    )
    with pytest.raises(PlanError, match="not declared"):
        declared_harness_root(subject, "pkg/repository-spec.yaml", "src/other")


def test_ambiguous_roots_need_a_choice(tmp_path: Path) -> None:
    subject = _spec(
        tmp_path,
        "repository_structure:\n  conformance_harness_roots:\n  - src/a/\n  - src/b/\n",
    )
    with pytest.raises(PlanError, match="plan must name one"):
        declared_harness_root(subject, "pkg/repository-spec.yaml", None)
    assert declared_harness_root(subject, "pkg/repository-spec.yaml", "src/b") == "src/b"


def test_declared_in_must_be_contained(tmp_path: Path) -> None:
    subject = _spec(tmp_path, "repository_structure: {}\n")
    with pytest.raises(PlanError, match="contained"):
        declared_harness_root(subject, "../outside.yaml", None)


def test_stage_source_subject_binds_every_file(tmp_path: Path) -> None:
    subject = tmp_path / "subject"
    (subject / "src" / "pkg" / "sub").mkdir(parents=True)
    (subject / "src" / "pkg" / "__init__.py").write_bytes(b"")
    (subject / "src" / "pkg" / "sub" / "m.py").write_bytes(b"Y = 1\n")
    (subject / "src" / "other").mkdir()
    (subject / "src" / "other" / "__init__.py").write_bytes(b"")
    tree, manifest, digest = stage_source_subject(
        subject, "Org/r@" + "b" * 40, "src", ["pkg"], tmp_path / "staged"
    )
    from l9_conformance import node_artifact

    art = node_artifact.load(tree, manifest, digest)
    assert [f["path"] for f in art.files] == ["pkg/__init__.py", "pkg/sub/m.py"]
    assert not (tree / "other").exists()


def test_stage_source_subject_refuses_bytecode(tmp_path: Path) -> None:
    subject = tmp_path / "subject"
    (subject / "src" / "pkg" / "__pycache__").mkdir(parents=True)
    (subject / "src" / "pkg" / "__init__.py").write_bytes(b"")
    (subject / "src" / "pkg" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    with pytest.raises(PlanError, match="bytecode"):
        stage_source_subject(subject, "Org/r@" + "b" * 40, "src", ["pkg"], tmp_path / "s")
    with pytest.raises(PlanError, match="contained"):
        stage_source_subject(subject, "Org/r@" + "b" * 40, "../x", ["pkg"], tmp_path / "s")
