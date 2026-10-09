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
