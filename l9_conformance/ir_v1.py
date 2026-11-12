"""Adapter for ``l9.conformance-ir/v1`` (Quantum-L9/.github F1, not admitted).

Source: ``semantics/conformance_ir.schema.yaml`` at .github@44e02a09 (F1 v3.40.0).

The v1 IR states obligations, their witnessing cases, dispositions and unresolved
reasons. It does not carry the rendered suite, a per-profile digest, or whether its
authority revision is admitted. Those arrive in a caller-digest-bound **suite binding**
(``l9-conformance/suite-binding@1``) naming the compiler's pytest/v2 rendering: four
files in one flat directory, each bound by raw sha256. A witness case id maps to the
test ``test_l9_conformance.py::<identifier(case_id)>``; its verdict is read from the
suite's outcome record (``L9_CONFORMANCE_OUTCOMES``), keyed by the exact case id.

Parsing is strict and fail-closed: anything the adapter cannot interpret faithfully is
an :class:`ir.IRInvalid`. It never invents an obligation, a case, or a digest.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, cast

import yaml

from l9_conformance import ir
from l9_conformance.digests import sha256_canonical

SCHEMA: Final = "l9.conformance-ir/v1"
SUITE_BINDING_SCHEMA: Final = "l9-conformance/suite-binding@1"
PROFILE_DIGEST_FROM_CATALOG: Final = ""  # v1 profile refs carry no digest; runner binds file bytes
_SUBJECT_CLASSES: Final = frozenset(
    {"product_invariant", "admitted_contract", "admitted_invariant"}
)
_DISPOSITIONS: Final = frozenset({"executable", "unresolved"})
_UNRESOLVED_RESULTS: Final = frozenset({"unresolved", "incompatible", "unsat", "ambiguous"})
_VERDICT_RULE: Final = "pass_only_when_every_witness_case_executes_and_every_assertion_holds"


PYTEST_V2_FILES: Final = frozenset(
    {"conftest.py", "test_l9_conformance.py", "l9_conformance_cases.json", "l9_conformance_eval.py"}
)
PYTEST_V2_TEST_MODULE: Final = "test_l9_conformance.py"
PYTEST_V2_CASES_FILE: Final = "l9_conformance_cases.json"


@dataclass(frozen=True)
class SuiteBinding:
    """The compiler's pytest/v2 rendered suite, bound by raw sha256 per file."""

    files: tuple[ir.SuiteFile, ...]
    ir_ref: str
    fixture_ir_ref: str
    canonical_admitted: bool


def identifier(case_id: str) -> str:
    """The compiler's pytest/v2 test name for a case id (``_identifier``)."""
    return "test_" + "".join(
        c if (c.isascii() and (c.isalnum() or c == "_")) else "_" for c in case_id
    )


def _m(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ir.IRInvalid(f"{where}: expected mapping")
    return cast(Mapping[str, Any], value)


def _l(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise ir.IRInvalid(f"{where}: expected list")
    return cast(list[Any], value)


def _s(mapping: Mapping[str, Any], key: str, where: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ir.IRInvalid(f"{where}.{key}: expected non-empty string")
    return value


def _digest(mapping: Mapping[str, Any], key: str, where: str) -> str:
    value = _s(mapping, key, where)
    hexpart = value.removeprefix("sha256:")
    if (
        not value.startswith("sha256:")
        or len(hexpart) != 64
        or any(c not in "0123456789abcdef" for c in hexpart)
    ):
        raise ir.IRInvalid(f"{where}.{key}: expected sha256:<64 hex>")
    return value


def load_suite_binding(data: bytes) -> SuiteBinding:
    try:
        doc = _m(yaml.safe_load(data), "suite binding")
    except yaml.YAMLError as exc:
        raise ir.IRInvalid(f"suite binding: unparseable: {exc}") from exc
    if doc.get("schema") != SUITE_BINDING_SCHEMA:
        raise ir.IRInvalid(f"suite binding schema must be {SUITE_BINDING_SCHEMA}")
    if doc.get("layout") != "pytest/v2":
        raise ir.IRInvalid("suite binding layout must be pytest/v2")
    admitted = doc.get("canonical_admitted")
    if not isinstance(admitted, bool):
        raise ir.IRInvalid("suite binding canonical_admitted: expected boolean")
    files: list[ir.SuiteFile] = []
    for i, raw in enumerate(_l(doc.get("files"), "suite binding files")):
        f = _m(raw, f"suite binding files[{i}]")
        path = _s(f, "path", f"suite binding files[{i}]")
        files.append(ir.SuiteFile(path=path, sha256=_digest(f, "sha256", f"suite files[{i}]")))
    names = [f.path for f in files]
    if len(names) != len(set(names)) or set(names) != set(PYTEST_V2_FILES):
        raise ir.IRInvalid(
            f"suite binding files must be exactly {sorted(PYTEST_V2_FILES)}, got {sorted(names)}"
        )
    return SuiteBinding(
        files=tuple(sorted(files, key=lambda f: f.path)),
        ir_ref=_s(doc, "ir_ref", "suite binding"),
        fixture_ir_ref=_s(doc, "fixture_ir_ref", "suite binding"),
        canonical_admitted=admitted,
    )


def _authority(value: str) -> tuple[str, str]:
    repo, sep, rev = value.rpartition("@")
    if not sep or not repo or len(rev) != 40 or any(c not in "0123456789abcdef" for c in rev):
        raise ir.IRInvalid(f"ir.authority.authority_revision {value!r} is not <repo>@<40-hex>")
    return repo, rev


def parse(document: Mapping[str, Any], binding: SuiteBinding) -> ir.ConformanceIR:
    if document.get("schema") != SCHEMA:
        raise ir.IRInvalid(f"ir.schema must be {SCHEMA}")
    subject = _m(document.get("subject"), "ir.subject")
    authority = _m(document.get("authority"), "ir.authority")
    if authority.get("compilation_profile_ref") != "l9.compilation/product-build@1":
        raise ir.IRInvalid(
            "ir.authority.compilation_profile_ref is not l9.compilation/product-build@1"
        )
    if authority.get("stage") != "conformance":
        raise ir.IRInvalid("ir.authority.stage is not conformance")
    repo, revision = _authority(_s(authority, "authority_revision", "ir.authority"))

    profile = _m(document.get("fixture_profile"), "ir.fixture_profile")
    status = profile.get("status")
    refs = [str(r) for r in _l(profile.get("profile_refs"), "ir.fixture_profile.profile_refs")]
    if status not in ("declared", "absent"):
        raise ir.IRInvalid("ir.fixture_profile.status must be declared or absent")
    if status == "declared" and not refs:
        raise ir.IRInvalid("ir.fixture_profile declared without profile_refs")
    if status == "absent" and refs:
        raise ir.IRInvalid("ir.fixture_profile absent but profile_refs present")

    unresolved_reasons: dict[str, str] = {}
    for i, raw in enumerate(_l(document.get("unresolved", []), "ir.unresolved")):
        u = _m(raw, f"ir.unresolved[{i}]")
        result = u.get("result")
        if result not in _UNRESOLVED_RESULTS:
            raise ir.IRInvalid(f"ir.unresolved[{i}].result {result!r} not in closed set")
        ref = _s(u, "obligation_ref", f"ir.unresolved[{i}]")
        unresolved_reasons[ref] = f"{result}: {_s(u, 'reason', f'ir.unresolved[{i}]')}"

    obligations: list[ir.Obligation] = []
    seen: set[str] = set()
    for i, raw in enumerate(_l(document.get("obligations"), "ir.obligations")):
        where = f"ir.obligations[{i}]"
        o = _m(raw, where)
        oid = _s(o, "id", where)
        if oid in seen:
            raise ir.IRInvalid(f"{where}.id {oid!r} is duplicated")
        seen.add(oid)
        if o.get("subject_class") not in _SUBJECT_CLASSES:
            raise ir.IRInvalid(f"{where}.subject_class not in closed set")
        if not _l(o.get("governing_refs"), f"{where}.governing_refs"):
            raise ir.IRInvalid(f"{where}.governing_refs must not be empty")
        vclass = o.get("verification_class")
        if vclass not in ir.VERIFICATION_CLASSES:
            raise ir.IRInvalid(f"{where}.verification_class not in closed set")
        disposition = o.get("disposition")
        if disposition not in _DISPOSITIONS:
            raise ir.IRInvalid(f"{where}.disposition not in closed set")
        witnesses = [str(w) for w in _l(o.get("witnessed_by"), f"{where}.witnessed_by")]
        cases: list[ir.Case] = []
        if disposition == "executable":
            if vclass != "fixture_replay" or not witnesses:
                raise ir.IRInvalid(f"{where}: executable requires fixture_replay and witnesses")
            if oid in unresolved_reasons:
                raise ir.IRInvalid(f"{where}: executable obligation listed as unresolved")
            plan = _m(o.get("execution_plan"), f"{where}.execution_plan")
            if (
                plan.get("mode") != "fixture_replay"
                or plan.get("isolation") != "fresh_subject_per_case"
                or plan.get("verdict_rule") != _VERDICT_RULE
            ):
                raise ir.IRInvalid(f"{where}.execution_plan is not the fixture_replay plan")
            plan_cases = [str(c) for c in _l(plan.get("cases"), f"{where}.execution_plan.cases")]
            if sorted(plan_cases) != sorted(witnesses):
                raise ir.IRInvalid(f"{where}.execution_plan.cases differ from witnessed_by")
            for case_id in plan_cases:
                nodeid = f"{PYTEST_V2_TEST_MODULE}::{identifier(case_id)}"
                cases.append(ir.Case(case_id=case_id, nodeid=ir.checked_nodeid(nodeid)))
        elif oid not in unresolved_reasons:
            raise ir.IRInvalid(f"{where}: unresolved obligation not listed in ir.unresolved")
        obligations.append(
            ir.Obligation(
                obligation_id=oid,
                verification_class=cast(ir.VerificationClass, vclass),
                required=True,
                cases=tuple(cases),
                disposition=str(disposition),
                unresolved_reason=unresolved_reasons.get(oid),
            )
        )
    unknown_refs = set(unresolved_reasons) - seen
    if unknown_refs:
        raise ir.IRInvalid(f"ir.unresolved names unknown obligations: {sorted(unknown_refs)}")
    # A case id maps to one test name; two distinct ids must not collide (the compiler
    # refuses this too). The same case id may witness more than one obligation.
    names: dict[str, str] = {}
    for obligation in obligations:
        for case in obligation.cases:
            prior = names.setdefault(case.nodeid, case.case_id)
            if prior != case.case_id:
                raise ir.IRInvalid(
                    f"case ids {prior!r} and {case.case_id!r} collide as {case.nodeid}"
                )

    return ir.ConformanceIR(
        schema=SCHEMA,
        synthetic=False,
        ir_ref=binding.ir_ref,
        product_ref=_s(subject, "product_ref", "ir.subject"),
        fixture_ir=ir.RefDigest(
            ref=binding.fixture_ir_ref, digest=_digest(subject, "fixture_ir_digest", "ir.subject")
        ),
        profiles=tuple(ir.RefDigest(ref=r, digest=PROFILE_DIGEST_FROM_CATALOG) for r in refs),
        canonical_repository=repo,
        canonical_revision=revision,
        canonical_admitted=binding.canonical_admitted,
        semantic_sources=(),
        execution_mode="fixture_replay",
        isolation="fresh_subject_per_case",
        suite_files=binding.files,
        obligations=tuple(obligations),
        fixture_digest_kind="semantic",
        semantic_digest=sha256_canonical(document),
        outcome_source="outcome_record",
        closure_digest=_digest(subject, "closure_digest", "ir.subject"),
    )


def load(data: bytes, binding: SuiteBinding) -> ir.ConformanceIR:
    try:
        document = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise ir.IRInvalid(f"ir: unparseable: {exc}") from exc
    return parse(_m(document, "ir"), binding)
