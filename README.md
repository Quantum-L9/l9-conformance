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
