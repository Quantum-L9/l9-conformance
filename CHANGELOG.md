# Changelog

## 0.2.0

- The runner consumes the compiler's conformance closure `l9.conformance-closure/v1`
  (l9-semantic-compiler 0.16.0 `birth-closure`, campaign L9-NODE-PIPELINE-01 lane C):
  `scripts/run_conformance.py` (fixed work dir: `work/closure/`, `work/product/` in,
  `work/receipt.json` out; no path arguments, only `--timeout`), built on
  the `l9.conformance-ir/v1` adapter and the ruling-D5 harness binding. Every digest the
  closure names is verified fail-closed; the driver module must match the closure's digest.
- Birth gate: every receipt carries `observation_or_outcome.birth_gate` (`birth_passable`,
  `reasons`, `open_evidence_obligations`, `rule`) and per-obligation `governing_laws`; see
  README. Unresolved verdicts are never rewritten. `overall_verdict` and exit codes unchanged.
- Compiler node manifests: `birth.artifacts[]` files are bound by raw digest.
- Receipt `l9-conformance/conformance-receipt@1`: optional `governing_coordinates.conformance_closure`,
  `observation_or_outcome.birth_gate`, obligation `governing_laws`.
- `scripts/run_conformance.py` takes no path arguments (SonarCloud pythonsecurity:S8707,
  S8705): the former individual-input CLI (`--ir ... --out ...`) is removed; that form stays
  available through the Python API and `scripts/rehearse.py`.
- `.sonarcloud.properties` excludes `testdata/` (verbatim compiler output) from analysis
  and duplication.
- `scripts/rehearse.py` takes no path arguments (SonarCloud pythonsecurity:S8707): plan and
  receipt live in the fixed work directory `rehearsal/`. Receipts recorded earlier name the
  former `--plan <plan>` reproduction.
- SonarCloud cleanups without behavior change (S3776 splits, S1192, S1172, S6353, S5778, S9073).
- Test data: real compiled closure `testdata/compiled-closures/l9-state-711da68`
  (compiler 57f91c0) and `scripts/regen_compiled_closure.py --check`.
- Also in this release (PR #4): the `l9.conformance-ir/v1` adapter, pytest/v2 outcome-record
  verdicts, harness binding (ruling D5), governing semantic sources, source-subject node
  manifests, `scripts/rehearse.py`, and the M2 State trial receipt.

## 0.1.0

- Receipt schema, fail-closed runner (synthetic conformance IR), candidate
  authoritative-state profiles.
