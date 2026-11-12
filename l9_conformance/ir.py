"""Conformance IR adapter.

The runner's only view of a conformance IR is the :class:`ConformanceIR` value
below. The real input format is the F1 candidate ``l9.conformance-ir/v1``
(Quantum-L9/.github, not admitted at the time of writing). Until its bytes are
bound, the only accepted format is the *synthetic* test format
``l9-conformance/synthetic-conformance-ir@0``, which exists solely so the
runner can be exercised; receipts produced from it are graded ``synthetic_test``.

The adapter validates shape and refuses anything it cannot interpret. It never
fills in a missing obligation, case, or digest.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast

import yaml

SYNTHETIC_SCHEMA: Final = "l9-conformance/synthetic-conformance-ir@0"
SUPPORTED_SCHEMAS: Final = frozenset({SYNTHETIC_SCHEMA})
VERIFICATION_CLASSES: Final = frozenset(
    {"fixture_replay", "static_inspection", "external_runtime_evidence"}
)

VerificationClass = Literal["fixture_replay", "static_inspection", "external_runtime_evidence"]


# A pytest node id restricted to "<relative/path>.py::<name>[::<name>...]" with an
# optional parametrization suffix. No option-like or path-escaping forms are accepted.
_NODEID: Final = re.compile(
    r"^(?!-)(?:[A-Za-z0-9_][A-Za-z0-9_.-]*/)*[A-Za-z0-9_][A-Za-z0-9_.-]*\.py"
    r"(?:::[A-Za-z_][A-Za-z0-9_]*)+(?:\[[A-Za-z0-9_.,=+-]*\])?$"
)


class IRInvalid(ValueError):
    """Raised when an IR cannot be interpreted faithfully."""


@dataclass(frozen=True)
class RefDigest:
    ref: str
    digest: str


@dataclass(frozen=True)
class Case:
    case_id: str
    nodeid: str


@dataclass(frozen=True)
class Obligation:
    obligation_id: str
    verification_class: VerificationClass
    required: bool
    cases: tuple[Case, ...]


@dataclass(frozen=True)
class SuiteFile:
    path: str
    sha256: str


@dataclass(frozen=True)
class ConformanceIR:
    schema: str
    synthetic: bool
    ir_ref: str
    product_ref: str
    fixture_ir: RefDigest
    profiles: tuple[RefDigest, ...]
    canonical_repository: str
    canonical_revision: str
    canonical_admitted: bool
    semantic_sources: tuple[RefDigest, ...]
    execution_mode: str
    isolation: str
    suite_files: tuple[SuiteFile, ...]
    obligations: tuple[Obligation, ...]


def checked_nodeid(nodeid: str) -> str:
    """Return ``nodeid`` if it is a plain contained pytest node id, else raise IRInvalid."""
    if not _NODEID.fullmatch(nodeid) or ".." in nodeid.split("::", 1)[0].split("/"):
        raise IRInvalid(f"case nodeid {nodeid!r} is not a plain contained pytest node id")
    return nodeid


def _req(mapping: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in mapping or mapping[key] is None:
        raise IRInvalid(f"{where}: missing required field {key!r}")
    return mapping[key]


def _str(mapping: Mapping[str, Any], key: str, where: str) -> str:
    value = _req(mapping, key, where)
    if not isinstance(value, str) or not value:
        raise IRInvalid(f"{where}.{key}: expected non-empty string")
    return value


def _digest(mapping: Mapping[str, Any], key: str, where: str) -> str:
    value = _str(mapping, key, where)
    hexpart = value.removeprefix("sha256:")
    if (
        not value.startswith("sha256:")
        or len(hexpart) != 64
        or any(c not in "0123456789abcdef" for c in hexpart)
    ):
        raise IRInvalid(f"{where}.{key}: expected sha256:<64 hex>")
    return value


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise IRInvalid(f"{where}: expected mapping")
    return cast(Mapping[str, Any], value)


def _list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise IRInvalid(f"{where}: expected list")
    return cast(list[Any], value)


def _ref_digest(value: Any, where: str) -> RefDigest:
    m = _mapping(value, where)
    return RefDigest(ref=_str(m, "ref", where), digest=_digest(m, "digest", where))


def parse(document: Mapping[str, Any]) -> ConformanceIR:
    schema = _str(document, "schema", "ir")
    if schema not in SUPPORTED_SCHEMAS:
        raise IRInvalid(f"ir.schema {schema!r} is not a supported conformance IR format")
    synthetic = schema == SYNTHETIC_SCHEMA
    if synthetic and document.get("synthetic") is not True:
        raise IRInvalid("synthetic IR must declare synthetic: true")
    authority = _mapping(_req(document, "canonical_authority", "ir"), "ir.canonical_authority")
    admitted = _req(authority, "admitted", "ir.canonical_authority")
    if not isinstance(admitted, bool):
        raise IRInvalid("ir.canonical_authority.admitted: expected boolean")
    revision = _str(authority, "revision", "ir.canonical_authority")
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise IRInvalid("ir.canonical_authority.revision: expected 40-hex commit")
    plan = _mapping(_req(document, "execution_plan", "ir"), "ir.execution_plan")
    mode = _str(plan, "mode", "ir.execution_plan")
    isolation = _str(plan, "isolation", "ir.execution_plan")
    if mode != "fixture_replay":
        raise IRInvalid(f"ir.execution_plan.mode {mode!r} is not executable by this runner")
    if isolation != "fresh_subject_per_case":
        raise IRInvalid(f"ir.execution_plan.isolation {isolation!r} is not supported")
    suite = _mapping(_req(document, "rendered_suite", "ir"), "ir.rendered_suite")
    suite_files: list[SuiteFile] = []
    for i, raw in enumerate(
        _list(_req(suite, "files", "ir.rendered_suite"), "ir.rendered_suite.files")
    ):
        f = _mapping(raw, f"ir.rendered_suite.files[{i}]")
        path = _str(f, "path", f"ir.rendered_suite.files[{i}]")
        if Path(path).is_absolute() or ".." in Path(path).parts:
            raise IRInvalid(f"ir.rendered_suite.files[{i}].path must be relative and contained")
        suite_files.append(
            SuiteFile(path=path, sha256=_digest(f, "sha256", f"ir.rendered_suite.files[{i}]"))
        )
    if not suite_files:
        raise IRInvalid("ir.rendered_suite.files: must not be empty")
    obligations: list[Obligation] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(_list(_req(document, "obligations", "ir"), "ir.obligations")):
        where = f"ir.obligations[{i}]"
        o = _mapping(raw, where)
        oid = _str(o, "id", where)
        if oid in seen_ids:
            raise IRInvalid(f"{where}.id {oid!r} is duplicated")
        seen_ids.add(oid)
        vclass = _str(o, "verification_class", where)
        if vclass not in VERIFICATION_CLASSES:
            raise IRInvalid(f"{where}.verification_class {vclass!r} is not in the closed set")
        required = _req(o, "required", where)
        if not isinstance(required, bool):
            raise IRInvalid(f"{where}.required: expected boolean")
        cases: list[Case] = []
        for j, rc in enumerate(_list(o.get("cases", []), f"{where}.cases")):
            c = _mapping(rc, f"{where}.cases[{j}]")
            cases.append(
                Case(
                    case_id=_str(c, "case_id", f"{where}.cases[{j}]"),
                    nodeid=checked_nodeid(_str(c, "nodeid", f"{where}.cases[{j}]")),
                )
            )
        obligations.append(
            Obligation(
                obligation_id=oid,
                verification_class=cast(VerificationClass, vclass),
                required=required,
                cases=tuple(cases),
            )
        )
    subject = _mapping(_req(document, "subject", "ir"), "ir.subject")
    return ConformanceIR(
        schema=schema,
        synthetic=synthetic,
        ir_ref=_str(document, "ir_ref", "ir"),
        product_ref=_str(subject, "product_ref", "ir.subject"),
        fixture_ir=_ref_digest(_req(document, "fixture_ir", "ir"), "ir.fixture_ir"),
        profiles=tuple(
            _ref_digest(p, f"ir.profiles[{i}]")
            for i, p in enumerate(_list(document.get("profiles", []), "ir.profiles"))
        ),
        canonical_repository=_str(authority, "repository", "ir.canonical_authority"),
        canonical_revision=revision,
        canonical_admitted=admitted,
        semantic_sources=tuple(
            _ref_digest(s, f"ir.semantic_source_digests[{i}]")
            for i, s in enumerate(
                _list(document.get("semantic_source_digests", []), "ir.semantic_source_digests")
            )
        ),
        execution_mode=mode,
        isolation=isolation,
        suite_files=tuple(suite_files),
        obligations=tuple(obligations),
    )


def load(data: bytes) -> ConformanceIR:
    try:
        document = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise IRInvalid(f"ir: unparseable: {exc}") from exc
    return parse(_mapping(document, "ir"))
