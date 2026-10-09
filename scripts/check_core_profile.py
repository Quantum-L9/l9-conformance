#!/usr/bin/env python3
"""Execute the core fixture witness.

The expected class is the one global law already requires of
l9.dependency-archetype/semantic-subsystem@1. This script does not import
l9-graphite-memory and does not define a new requirement.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "catalog" / "fixtures" / "semantic-subsystem-requires-core.yaml"
PROFILE = ROOT / "catalog" / "fixture-profile-core-1.yaml"
REQUIRED_CLASS = "core"
ARCHETYPE = "l9.dependency-archetype/semantic-subsystem@1"
PROFILE_ID = "l9.fixture-profile/core@1"


def main() -> int:
    fixture = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    profile = yaml.safe_load(PROFILE.read_text(encoding="utf-8"))
    failures: list[str] = []
    if fixture.get("conformance_class") != REQUIRED_CLASS:
        failures.append("fixture conformance_class is not core")
    if fixture.get("observable", {}).get("archetype_id") != ARCHETYPE:
        failures.append("fixture does not witness the semantic-subsystem archetype")
    classes = fixture.get("observable", {}).get("required_conformance_classes") or []
    if REQUIRED_CLASS not in classes:
        failures.append("fixture does not record required class core")
    if fixture.get("expected", {}).get("conformance_class_present") != REQUIRED_CLASS:
        failures.append("fixture expected result is not class core")
    if profile.get("id") != PROFILE_ID:
        failures.append("profile id is not l9.fixture-profile/core@1")
    if profile.get("conformance_class") != REQUIRED_CLASS:
        failures.append("profile class is not core")
    if fixture["id"] not in (profile.get("membership") or []):
        failures.append("profile membership does not contain the core fixture")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(f"PASS: {PROFILE_ID} membership witnesses {ARCHETYPE} class {REQUIRED_CLASS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
