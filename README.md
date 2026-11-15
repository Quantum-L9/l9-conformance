# l9-conformance

Executable conformance owner for published fixture profiles.

This repository admits and publishes conformance profiles. It does not own global constitutional law, and it does not admit memory releases. `Quantum-L9/.github` records the reachable profile coordinate so consumers can resolve it. The profile body stays here.

## Admitted profile

| Identity | Class | Scope | Digest |
|---|---|---|---|
| `l9.fixture-profile/core@1` | `core` | `l9.dependency-archetype/semantic-subsystem@1` | `sha256:3d2cb01a2b872215a3e2298b4e3b3d7ae99ecf867d6486f5730a3f98aba2bb2f` |

The profile is published and resolvable. Its membership is the normative fixture `l9.fixture/semantic-subsystem-requires-core@1`, which witnesses the conformance class `core` already required by that archetype in `semantics/dependency_archetypes.yaml` blob `b0e4125d80c30dae38e0d6b8a36b2d394f6aa79a`. The fixture does not create a requirement and does not describe `l9-graphiti-memory` behavior.

Check the witness:

```bash
python3 scripts/check_core_profile.py
```

## Candidate node-class profiles (not admitted)

Archetype `l9.node-archetype/authoritative-state@1` requires conformance classes `core`, `capability`, `runtime` and `adapter` (`semantics/node_archetypes.yaml`, Quantum-L9/.github `dcd9d009`, blob `c1d0b422`). One profile per class, each versioned independently:

| Identity | Class | Status |
|---|---|---|
| `l9.fixture-profile/authoritative-state-core@1` | `core` | candidate |
| `l9.fixture-profile/authoritative-state-capability@1` | `capability` | candidate |
| `l9.fixture-profile/authoritative-state-runtime@1` | `runtime` | candidate |
| `l9.fixture-profile/authoritative-state-adapter@1` | `adapter` | candidate |

Each profile's membership is one normative fixture witnessing that the archetype requires the class. Governing law does not say what each class obliges, which archetype requirements belong to which class, or which product-declared fixture sets exist; each profile records these as `unresolved` entries instead of inventing them. Digests are in `catalog/INDEX.yaml` and `SHA256SUMS`.

```bash
python3 scripts/check_catalog.py                       # integrity
python3 scripts/check_catalog.py --law-root <.github>  # plus trace to pinned law
```

## Conformance runner and receipts

`scripts/run_conformance.py` verifies every bound digest (conformance IR, fixture IR, catalog profiles, rendered suite, node artifact), runs each case in a fresh temporary directory and process, and emits an `l9-conformance/conformance-receipt@1` receipt (`schemas/conformance-receipt-1.schema.json`). A receipt is evidence only: it is not admission and grants no authority.

### Compiler closures (`l9.conformance-closure/v1`)

Since 0.2.0 the runner consumes the conformance closure `l9-semantic-compiler birth-closure` (0.16.0+) writes. The script takes no path arguments: inputs and output live under the fixed, git-ignored work directory `work/` at the repository root.

```bash
# copy (or link) the compiler's <out>/conformance/ to work/closure, and the product checkout to work/product
cp -r <out>/conformance work/closure
cp -r <product checkout> work/product
python3 scripts/run_conformance.py [--timeout SECONDS]   # writes work/receipt.json
```

A receipt from an earlier run is removed first, so a refusal (exit 2) leaves none. Other input forms (synthetic IR, a v1 IR with a suite binding) are the Python API (`l9_conformance.runner.RunRequest`) and `scripts/rehearse.py`. Every case the IR plans must be a case of the suite's own case table before anything executes.

Every binding comes from the closure and is verified before anything executes, failing closed on any mismatch or unknown schema/status: the closure fields and contained paths; the conformance IR raw digest and its coordinates (product, fixture IR and closure digests, authority revision, profile refs); the fixture IR semantic digest, subject and `law_index`; the `pytest/v2` suite file set byte-exact and its case table; the node artifact (manifest semantic digest, compilation receipt, exact file set including `birth.artifacts[]`). The product root supplies only the conformance driver harness, bound per ruling D5 (every file under the declared harness root, recorded in `governing_coordinates.harness`); the driver module must hold exactly the closure's `driver_factory.digest`. Cases then run exactly as for a v1 IR (one fresh directory and process per case, verdict from the suite's outcome record cross-checked with JUnit). The receipt records the closure in `governing_coordinates.conformance_closure`. Closures over the v3.40.0 candidate schemas are `candidate_not_admitted`, so the grade is at most `candidate_authority_rehearsal`.

### Birth gate (`observation_or_outcome.birth_gate`)

Every receipt carries a birth-gate classification (CTO ruling, campaign L9-NODE-PIPELINE-01), derived from the recorded obligations alone and re-derived by `verify_receipt`:

- `birth_passable` is true iff no obligation is `fail` or `error`, and every `unresolved` obligation is governed only by laws whose declared `verification_class` in the product's fixture IR `law_index` is `static_inspection` or `external_runtime_evidence` (recorded per obligation in `governing_laws`).
- Those obligations stay `unresolved` (never rewritten to `pass`) and are listed in `open_evidence_obligations`.
- An unresolved `fixture_replay` obligation, or an unresolved obligation whose law is not in the law index (`verification_class: null`, or no `governing_laws`), is not passable. `reasons` names every blocker.
- An obligation's verification class must equal its law's; an IR that relabels one refuses.

`overall_verdict` and the exit code (0 only on `pass`) are unchanged. The birth gate is evidence classification only: it is not admission and grants no authority.

Test data: `testdata/compiled-closures/` is real compiler output (`PROVENANCE.md`; check with `scripts/regen_compiled_closure.py --check`).

### Rehearsal (`scripts/rehearse.py`)

The rehearsal driver takes no path arguments: it reads its plan from the fixed work directory `rehearsal/plan.yaml` (git-ignored) and writes the receipt to `rehearsal/receipt.json`. A plan carrying `out` is refused.
