"""Rehearsal driver: pinned-commit fetching (local repositories only, no network)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from rehearse import PlanError, fetch_pinned


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
