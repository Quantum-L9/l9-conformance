# ADR-001: Sole issuer of realization receipts; runner input binding and isolation

- **Status:** Accepted (2026-11-12)
- **Decision source:** operator — conformance is owned by the dedicated `l9-conformance`
  node, and the compiler's own run is `rehearsal_execution`, never a receipt;
  R5 coordinator rulings R1 and the Codex review findings on PR #2 (2026-11-12).

## Context

A compiled node counts as `realized` only on executable conformance evidence.
`Quantum-L9/.github` `semantics/conformance_model.yaml` assigns `conformance_receipts`
to `l9-conformance`. The runner executes a peer-supplied test suite against a peer-built
node artifact, so every input it trusts must be bound before any of that code runs.

## Decision

1. `l9-conformance` is the only issuer of the receipt that lets a node be `realized`.
   The compiler never self-certifies: its manifest records `realized: false`, and its
   rehearsal run is not a receipt.
2. A receipt (`schemas/conformance-receipt-1.schema.json`) binds the conformance-IR
   and fixture-IR digests, the profile refs and digests, the rendered suite files, and
   the exact node artifact files. It is content-addressed and immutable. It is
   evidence only and grants nothing: its required `authority` block fixes
   `grants_semantic_authority`, `grants_execution_authority` and `is_admission` to
   `false`, `canonical_authority.admitted` only records whether the input `.github`
   revision was admitted, and it carries no mutable currentness field.
3. The runner refuses before executing anything when an input is not bound:
   - each profile file's bytes are hashed and must match the IR digest;
   - catalog entry paths must stay inside `catalog/` (no absolute path, no `..`,
     no symlinked parent, checked after resolving);
   - a rendered-suite path listed twice is refused;
   - the suite and artifact trees must hold exactly the bound files.
4. Each case runs in a fresh temporary directory and process, with a scrubbed
   environment and `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, so plugins installed on the
   host never load.

## Consequences

- A compiler-produced result can reach `realized` only through a receipt from this
  repository.
- Suites must load any pytest plugin explicitly. A declared plugin channel in the IR
  is deferred until the compiler needs one.
- Isolation is process-level and is recorded honestly in each receipt. Per operator
  decision D3, it does not gate the evidence grade.

## Evidence

- `l9_conformance/runner.py`: `_profile_path` (catalog containment), `verify_inputs`
  (profile file digest, exact trees), case environment (`PYTEST_DISABLE_PLUGIN_AUTOLOAD`).
- `l9_conformance/ir.py`: duplicate suite path refusal. `l9_conformance/receipt.py`:
  content-addressed identity, independent re-derivation of verdicts.
- Tests in `tests/test_runner.py`: `test_profile_file_bytes_must_match`,
  `test_catalog_entry_path_must_stay_in_catalog`, `test_catalog_symlinked_parent_refused`,
  `test_duplicate_suite_path_refused`, `test_host_pytest_plugins_not_autoloaded`.
