"""Compiler conformance closure ``l9.conformance-closure/v1``: verify every binding, fail closed.

Producer: ``l9-semantic-compiler birth-closure`` (0.16.0, its ADR-0007), which writes
``<out>/conformance/closure.json`` beside ``conformance-ir.json`` (``l9.conformance-ir/v1``),
``fixture-ir.json`` (``l9.fixture-ir/v1``), the ``pytest/v2`` suite and the node artifact. The
schemas are the Quantum-L9/.github v3.40.0 candidates, so a closure is ``candidate_not_admitted``
and a run over it is at most a candidate-authority rehearsal.

This module verifies what the closure itself declares and adapts it to the runner's existing
v1 inputs; the runner then applies its own checks (IR parse, fixture IR semantic digest,
suite file set, node artifact, ruling-D5 harness binding):

* ``closure.json``: exact schema, every field typed, every path relative and contained;
* the conformance IR: raw-byte digest, then its coordinates against the closure (product,
  fixture IR and closure digests, authority revision, profile refs);
* the fixture IR: schema, subject, witness cases, and its ``law_index`` (law ref -> declared
  verification class), which the birth gate reads;
* the suite: ``suite_binding`` is ``pytest/v2`` and its case table names the same product
  and closure (its bytes are verified by the runner against ``suite_files``);
* the driver: ``driver_factory`` names ``module:factory`` whose module file is the one the
  spec imports; the ruling-D5 harness binding must hold that file at the closure's digest.

Nothing here admits, authorizes or reconciles: an unknown schema, status or field refuses.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, cast

from l9_conformance import harness, ir, ir_v1
from l9_conformance.digests import sha256_bytes, sha256_file

CLOSURE_SCHEMA: Final = "l9.conformance-closure/v1"
FIXTURE_IR_SCHEMA: Final = "l9.fixture-ir/v1"
SUITE_BINDING: Final = "pytest/v2"
# authority_status -> admitted. Only values this runner can interpret; any other refuses.
AUTHORITY_STATUS: Final[Mapping[str, bool]] = {"candidate_not_admitted": False}

_DRIVER_SPEC: Final = re.compile(r"^([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*):([A-Za-z_]\w*)$", re.ASCII)
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_CLOSURE: Final = "closure"
_COMPILER: Final = "closure.compiler"


class ClosureRefused(RuntimeError):
    """A closure binding could not be verified. Nothing executes and no receipt is emitted."""


@dataclass(frozen=True)
class DriverFactory:
    driver: str
    module_path: str
    digest: str
    harness_root: str
    import_root: str


@dataclass(frozen=True)
class Closure:
    path: Path
    digest: str  # raw bytes of closure.json
    product_ref: str
    ir_path: Path
    ir_digest: str
    fixture_ir_path: Path
    fixture_ir_digest: str
    suite_dir: Path
    suite_files: tuple[ir.SuiteFile, ...]
    artifact_dir: Path
    artifact_manifest_path: Path
    artifact_manifest_digest: str
    source_revision: str
    driver: DriverFactory
    profile_refs: tuple[str, ...]
    authority_revision: str
    authority_status: str
    product_manifest_closure_digest: str
    schema_sources: tuple[ir.RefDigest, ...]
    compiler: Mapping[str, str]


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ClosureRefused(f"{where}: expected mapping")
    return cast(Mapping[str, Any], value)


def _list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise ClosureRefused(f"{where}: expected list")
    return cast(list[Any], value)


def _str(mapping: Mapping[str, Any], key: str, where: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ClosureRefused(f"{where}.{key}: expected non-empty string")
    return value


def _digest(mapping: Mapping[str, Any], key: str, where: str) -> str:
    value = _str(mapping, key, where)
    if not value.startswith("sha256:") or not _HEX64.fullmatch(value.removeprefix("sha256:")):
        raise ClosureRefused(f"{where}.{key}: expected sha256:<64 hex>")
    return value


def _rel(value: str, where: str) -> str:
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or "\\" in value or not p.parts:
        raise ClosureRefused(f"{where}: path {value!r} must be relative and contained")
    return p.as_posix()


def _inside(base: Path, rel: str, where: str) -> Path:
    """``base/rel``, refusing a path that resolves (through symlinks) outside ``base``."""
    target = base / _rel(rel, where)
    if not target.resolve().is_relative_to(base.resolve()):
        raise ClosureRefused(f"{where}: {rel!r} resolves outside {base}")
    return target


def _json(path: Path, label: str) -> Any:
    def _no_constant(token: str) -> Any:
        raise ClosureRefused(f"{label}: non-finite number {token} is not canonical JSON")

    if not path.is_file() or path.is_symlink():
        raise ClosureRefused(f"{label}: {path} is missing or not a regular file")
    try:
        return json.loads(path.read_bytes(), parse_constant=_no_constant)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ClosureRefused(f"{label}: unparseable JSON: {exc}") from exc


def _driver(value: Any) -> DriverFactory:
    where = "closure.driver_factory"
    m = _mapping(value, where)
    driver = _str(m, "driver", where)
    if not _DRIVER_SPEC.fullmatch(driver):
        raise ClosureRefused(f"{where}.driver {driver!r} is not module:factory")
    factory = DriverFactory(
        driver=driver,
        module_path=_rel(_str(m, "module_path", where), f"{where}.module_path"),
        digest=_digest(m, "digest", where),
        harness_root=_rel(_str(m, "harness_root", where), f"{where}.harness_root"),
        import_root=_rel(_str(m, "import_root", where), f"{where}.import_root"),
    )
    harness_root = PurePosixPath(factory.harness_root)
    if harness_root.parent != PurePosixPath(factory.import_root):
        # Ruling D5 lays a harness out as <import root>/<package>: the harness root must be
        # one package directly under the import root.
        raise ClosureRefused(f"{where}: harness_root must be a package directly in import_root")
    dotted = driver.partition(":")[0].replace(".", "/")
    expected = {f"{factory.import_root}/{dotted}.py", f"{factory.import_root}/{dotted}/__init__.py"}
    if factory.module_path not in expected:
        raise ClosureRefused(
            f"{where}: module_path {factory.module_path} is not the file {driver} imports "
            f"from import_root {factory.import_root}"
        )
    return factory


def _suite_files(document: Mapping[str, Any]) -> tuple[ir.SuiteFile, ...]:
    files: list[ir.SuiteFile] = []
    for i, raw in enumerate(_list(document.get("suite_files"), "closure.suite_files")):
        where = f"closure.suite_files[{i}]"
        entry = _mapping(raw, where)
        path = _rel(_str(entry, "path", where), where)
        if path in {f.path for f in files}:
            raise ClosureRefused(f"{where}.path {path!r} is bound more than once")
        files.append(ir.SuiteFile(path=path, sha256=_digest(entry, "sha256", where)))
    if not files:
        raise ClosureRefused("closure.suite_files: must not be empty")
    return tuple(sorted(files, key=lambda f: f.path))


def _profile_refs(document: Mapping[str, Any]) -> tuple[str, ...]:
    refs: list[str] = []
    for i, ref in enumerate(_list(document.get("profile_refs"), "closure.profile_refs")):
        if not isinstance(ref, str) or not ref:
            raise ClosureRefused(f"closure.profile_refs[{i}]: expected non-empty string")
        refs.append(ref)
    return tuple(refs)


def _schema_sources(document: Mapping[str, Any], status: str) -> tuple[ir.RefDigest, ...]:
    binding = _mapping(document.get("schema_binding"), "closure.schema_binding")
    if _str(binding, "authority_status", "closure.schema_binding") != status:
        raise ClosureRefused("closure.schema_binding.authority_status differs from the closure's")
    schemas = _mapping(binding.get("schemas"), "closure.schema_binding.schemas")
    sources: list[ir.RefDigest] = []
    for key in sorted(schemas):
        entry = _mapping(schemas[key], f"schemas.{key}")
        sources.append(
            ir.RefDigest(
                ref=_str(entry, "ref", f"schemas.{key}"),
                digest=_digest(entry, "digest", f"schemas.{key}"),
            )
        )
    if not sources:
        raise ClosureRefused("closure.schema_binding.schemas: must not be empty")
    return tuple(sources)


def load(closure_path: Path) -> Closure:
    """Parse and structurally verify ``closure.json``; file bytes are checked later."""
    document = _mapping(_json(closure_path, _CLOSURE), _CLOSURE)
    if document.get("schema") != CLOSURE_SCHEMA:
        raise ClosureRefused(f"closure.schema {document.get('schema')!r} is not {CLOSURE_SCHEMA}")
    if _str(document, "suite_binding", _CLOSURE) != SUITE_BINDING:
        raise ClosureRefused(f"closure.suite_binding is not {SUITE_BINDING}")
    status = _str(document, "authority_status", _CLOSURE)
    if status not in AUTHORITY_STATUS:
        raise ClosureRefused(f"closure.authority_status {status!r} is not interpretable")
    compiler = _mapping(document.get("compiler"), _COMPILER)
    base = closure_path.parent

    def path(key: str) -> Path:
        return _inside(base, _str(document, key, _CLOSURE), f"closure.{key}")

    return Closure(
        path=closure_path,
        digest=sha256_file(closure_path),
        product_ref=_str(document, "product_ref", _CLOSURE),
        ir_path=path("ir"),
        ir_digest=_digest(document, "ir_digest", _CLOSURE),
        fixture_ir_path=path("fixture_ir"),
        fixture_ir_digest=_digest(document, "fixture_ir_digest", _CLOSURE),
        suite_dir=path("suite_dir"),
        suite_files=_suite_files(document),
        artifact_dir=path("artifact_dir"),
        artifact_manifest_path=path("artifact_manifest"),
        artifact_manifest_digest=_digest(document, "artifact_manifest_digest", _CLOSURE),
        source_revision=_str(document, "source_revision", _CLOSURE),
        driver=_driver(document.get("driver_factory")),
        profile_refs=_profile_refs(document),
        authority_revision=_str(document, "authority_revision", _CLOSURE),
        authority_status=status,
        product_manifest_closure_digest=_digest(
            document, "product_manifest_closure_digest", _CLOSURE
        ),
        schema_sources=_schema_sources(document, status),
        compiler={
            "name": _str(compiler, "name", _COMPILER),
            "version": _str(compiler, "version", _COMPILER),
        },
    )


def check_ir(closure: Closure) -> None:
    """The IR's raw bytes and coordinates are exactly the closure's."""
    if not closure.ir_path.is_file() or closure.ir_path.is_symlink():
        raise ClosureRefused(f"conformance IR {closure.ir_path} is missing")
    actual = sha256_bytes(closure.ir_path.read_bytes())
    if actual != closure.ir_digest:
        raise ClosureRefused(
            f"conformance IR digest mismatch: bound {closure.ir_digest}, actual {actual}"
        )
    document = _mapping(_json(closure.ir_path, "conformance IR"), "conformance IR")
    subject = _mapping(document.get("subject"), "ir.subject")
    provenance = _mapping(document.get("provenance"), "ir.provenance")
    authority = _mapping(document.get("authority"), "ir.authority")
    if subject.get("product_ref") != closure.product_ref:
        raise ClosureRefused("ir.subject.product_ref is not the closure product")
    for where, mapping in (("ir.subject", subject), ("ir.provenance", provenance)):
        if mapping.get("fixture_ir_digest") != closure.fixture_ir_digest:
            raise ClosureRefused(f"{where}.fixture_ir_digest is not the closure's fixture IR")
        if mapping.get("closure_digest") != closure.product_manifest_closure_digest:
            raise ClosureRefused(f"{where}.closure_digest is not the closure's")
    if authority.get("authority_revision") != closure.authority_revision:
        raise ClosureRefused("ir.authority.authority_revision differs from the closure's")
    profile = _mapping(document.get("fixture_profile"), "ir.fixture_profile")
    if tuple(_list(profile.get("profile_refs"), "ir.fixture_profile")) != closure.profile_refs:
        raise ClosureRefused("ir.fixture_profile.profile_refs differ from the closure's")


def law_index(closure: Closure) -> dict[str, str]:
    """The fixture IR's law ref -> declared verification class (its semantic digest is
    verified by the runner); its subject must be the closure's."""
    document = _mapping(_json(closure.fixture_ir_path, "fixture IR"), "fixture IR")
    if document.get("schema") != FIXTURE_IR_SCHEMA:
        raise ClosureRefused(f"fixture IR schema is not {FIXTURE_IR_SCHEMA}")
    subject = _mapping(document.get("subject"), "fixture IR subject")
    if subject.get("product_ref") != closure.product_ref:
        raise ClosureRefused("fixture IR subject.product_ref is not the closure product")
    if subject.get("closure_digest") != closure.product_manifest_closure_digest:
        raise ClosureRefused("fixture IR subject.closure_digest is not the closure's")
    laws: dict[str, str] = {}
    for i, raw in enumerate(_list(document.get("law_index"), "fixture IR law_index")):
        entry = _mapping(raw, f"law_index[{i}]")
        ref = _str(entry, "law_ref", f"law_index[{i}]")
        vclass = _str(entry, "verification_class", f"law_index[{i}]")
        if vclass not in ir.VERIFICATION_CLASSES:
            raise ClosureRefused(f"law_index[{i}].verification_class {vclass!r} is not known")
        if ref in laws:
            raise ClosureRefused(f"fixture IR law_index lists {ref!r} twice")
        laws[ref] = vclass
    return laws


def check_suite_table(closure: Closure) -> None:
    """The rendered case table (bytes verified by the runner) names this product, and every
    case the IR plans to execute is a case of that table.

    A planned case id selects a test only after it is found in the closure's own case table,
    so no case-derived value reaches a subprocess unless the bound suite declares it.
    """
    table = _mapping(
        _json(closure.suite_dir / ir_v1.PYTEST_V2_CASES_FILE, "suite case table"), "case table"
    )
    if table.get("binding") != SUITE_BINDING:
        raise ClosureRefused(f"suite case table binding is not {SUITE_BINDING}")
    if table.get("product_ref") != closure.product_ref:
        raise ClosureRefused("suite case table product_ref is not the closure product")
    declared = {
        _str(_mapping(c, f"case table cases[{i}]"), "id", f"case table cases[{i}]")
        for i, c in enumerate(_list(table.get("cases"), "case table cases"))
    }
    document = _mapping(_json(closure.ir_path, "conformance IR"), "conformance IR")
    for i, raw in enumerate(_list(document.get("obligations"), "ir.obligations")):
        plan = _mapping(raw, f"ir.obligations[{i}]").get("execution_plan")
        if plan is None:
            continue
        cases = _list(_mapping(plan, f"ir.obligations[{i}].execution_plan").get("cases"), "cases")
        missing = sorted({str(c) for c in cases} - declared)
        if missing:
            raise ClosureRefused(
                f"ir.obligations[{i}]: planned cases {missing} are not in the suite case table"
            )


def check_source_revision(closure: Closure, supplied: str | None) -> None:
    if supplied is not None and supplied != closure.source_revision:
        raise ClosureRefused(
            f"source revision mismatch: supplied {supplied}, closure {closure.source_revision}"
        )


def suite_binding(closure: Closure) -> ir_v1.SuiteBinding:
    """The closure's pytest/v2 suite as the runner's v1 suite binding (same checks apply)."""
    names = {f.path for f in closure.suite_files}
    if names != set(ir_v1.PYTEST_V2_FILES):
        raise ClosureRefused(
            f"closure suite_files must be exactly {sorted(ir_v1.PYTEST_V2_FILES)}, "
            f"got {sorted(names)}"
        )
    return ir_v1.SuiteBinding(
        files=closure.suite_files,
        ir_ref=ir_v1.SCHEMA,
        fixture_ir_ref=FIXTURE_IR_SCHEMA,
        canonical_admitted=AUTHORITY_STATUS[closure.authority_status],
    )


def harness_request(closure: Closure, product_root: Path) -> harness.HarnessRequest:
    """The ruling-D5 harness request for the closure's driver under ``product_root``."""
    if not product_root.is_dir():
        raise ClosureRefused(f"product root {product_root} is not a directory")
    driver = closure.driver
    root_dir = _inside(product_root, driver.harness_root, "driver_factory.harness_root")
    for part in PurePosixPath(driver.harness_root).parents:
        if (product_root / part).is_symlink():
            raise ClosureRefused(f"harness root path component {part} is a symlink")
    return harness.HarnessRequest(
        root_dir=root_dir,
        root=driver.harness_root,
        source_revision=closure.source_revision,
        driver_ref=driver.driver,
        product_dir=product_root,
    )


def check_driver(closure: Closure, binding: harness.HarnessBinding) -> None:
    """The D5 binding must hold the driver module at exactly the closure's digest."""
    bound = {f["path"]: f["sha256"] for f in binding.files}
    actual = bound.get(closure.driver.module_path)
    if actual is None:
        raise ClosureRefused(f"driver module {closure.driver.module_path} is not in the harness")
    if actual != closure.driver.digest:
        raise ClosureRefused(
            f"driver module digest mismatch: bound {closure.driver.digest}, actual {actual}"
        )


def coordinates(closure: Closure) -> dict[str, Any]:
    """The receipt's ``governing_coordinates.conformance_closure`` block."""
    return {
        "schema": CLOSURE_SCHEMA,
        "digest": closure.digest,
        "suite_binding": SUITE_BINDING,
        "authority_status": closure.authority_status,
        "product_manifest_closure_digest": closure.product_manifest_closure_digest,
        "compiler": dict(closure.compiler),
    }
