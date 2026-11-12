#!/usr/bin/env python3
"""Check catalog integrity for every profile and fixture in catalog/INDEX.yaml.

Always checked (offline):
  * every INDEX digest equals sha256 of the file bytes and the SHA256SUMS line;
  * every SHA256SUMS line is listed in INDEX (no orphan digests);
  * profile id/class/membership in the file match the INDEX entry;
  * profile membership_digests (when present) match the fixture bytes;
  * each fixture's conformance_class equals its profile's class, is listed in
    observable.required_conformance_classes, and is the expected result.

With ``--law-root <Quantum-L9/.github checkout>`` additionally checked, for fixtures
that pin ``source_law.sha256``: the law file's sha256 matches, and the archetype the
fixture witnesses really requires that conformance class there. This is how a
fixture is shown to trace to existing law rather than invent it.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _yaml(path: Path) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def _sums(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, rel = line.split(maxsplit=1)
            out[rel.strip()] = "sha256:" + digest
    return out


def _archetype_classes(law_root: Path, rel: str, archetype: str) -> list[str] | None:
    doc = _yaml(law_root / rel)
    for entry in cast(list[Mapping[str, Any]], doc.get("archetypes") or []):
        if entry.get("id") == archetype:
            requires = cast(Mapping[str, Any], entry.get("requires") or {})
            return [str(c) for c in cast(list[Any], requires.get("conformance_classes") or [])]
    return None


def _check_law(fixture: Mapping[str, Any], law_root: Path, failures: list[str]) -> None:
    fid = fixture.get("id")
    source = cast(Mapping[str, Any], fixture.get("source_law") or {})
    pinned = source.get("sha256")
    if not isinstance(pinned, str):
        return
    rel = str(source.get("path"))
    law_file = law_root / rel
    if not law_file.is_file():
        failures.append(f"{fid}: law file {rel} missing under --law-root")
        return
    if _sha(law_file) != pinned:
        failures.append(f"{fid}: law file {rel} sha256 differs from pinned {pinned}")
        return
    observable = cast(Mapping[str, Any], fixture.get("observable") or {})
    archetype = str(observable.get("archetype_id"))
    classes = _archetype_classes(law_root, rel, archetype)
    if classes is None:
        failures.append(f"{fid}: archetype {archetype} not found in {rel}")
        return
    cls = fixture.get("conformance_class")
    if cls not in classes:
        failures.append(f"{fid}: {archetype} does not require class {cls} in {rel}")
    recorded = [str(c) for c in cast(list[Any], observable.get("required_conformance_classes"))]
    if sorted(recorded) != sorted(classes):
        failures.append(f"{fid}: observable classes {recorded} differ from law {classes}")


def check(root: Path, law_root: Path | None = None) -> list[str]:
    failures: list[str] = []
    index = _yaml(root / "catalog" / "INDEX.yaml")
    sums = _sums(root)
    listed: set[str] = set()

    def bound(rel: str, digest: str, what: str) -> bool:
        listed.add(rel)
        path = root / rel
        if not path.is_file():
            failures.append(f"{what}: {rel} missing")
            return False
        actual = _sha(path)
        if actual != digest:
            failures.append(f"{what}: {rel} digest {actual} != INDEX {digest}")
        if sums.get(rel) != digest:
            failures.append(f"{what}: {rel} SHA256SUMS entry {sums.get(rel)} != INDEX {digest}")
        return True

    for entry in cast(list[Mapping[str, Any]], index.get("profiles") or []):
        pid = str(entry["id"])
        if not bound(str(entry["path"]), str(entry["digest"]), pid):
            continue
        profile = _yaml(root / str(entry["path"]))
        if profile.get("id") != pid:
            failures.append(f"{pid}: file id {profile.get('id')} differs")
        if profile.get("conformance_class") != entry.get("conformance_class"):
            failures.append(f"{pid}: class differs between file and INDEX")
        if profile.get("status") != entry.get("status"):
            failures.append(f"{pid}: status differs between file and INDEX")
        members = cast(list[Mapping[str, Any]], entry.get("membership") or [])
        file_members = [str(m) for m in cast(list[Any], profile.get("membership") or [])]
        if [str(m["id"]) for m in members] != file_members:
            failures.append(f"{pid}: membership differs between file and INDEX")
        if not members:
            failures.append(f"{pid}: empty membership")
        member_digests = cast(Mapping[str, Any], profile.get("membership_digests") or {})
        for member in members:
            fid, frel, fdig = str(member["id"]), str(member["path"]), str(member["digest"])
            if not bound(frel, fdig, f"{pid} member {fid}"):
                continue
            if member_digests and member_digests.get(fid) != fdig:
                failures.append(f"{pid}: membership_digests[{fid}] != INDEX {fdig}")
            fixture = _yaml(root / frel)
            if fixture.get("id") != fid:
                failures.append(f"{fid}: file id {fixture.get('id')} differs")
            cls = fixture.get("conformance_class")
            if cls != profile.get("conformance_class"):
                failures.append(f"{fid}: class {cls} differs from profile {pid}")
            observable = cast(Mapping[str, Any], fixture.get("observable") or {})
            if cls not in cast(list[Any], observable.get("required_conformance_classes") or []):
                failures.append(f"{fid}: class {cls} not in observable required classes")
            expected = cast(Mapping[str, Any], fixture.get("expected") or {})
            if expected.get("conformance_class_present") != cls:
                failures.append(f"{fid}: expected result is not class {cls}")
            if law_root is not None:
                _check_law(fixture, law_root, failures)
    for rel in sorted(set(sums) - listed):
        failures.append(f"SHA256SUMS lists {rel}, which no INDEX entry binds")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--law-root", type=Path, default=None)
    args = parser.parse_args(argv)
    failures = check(args.root, args.law_root)
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    if failures:
        return 1
    scope = " with law trace" if args.law_root else ""
    print(f"PASS: catalog integrity{scope}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
