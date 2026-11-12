# AGENTS.md — l9-conformance

## Mission

Executable conformance owner (Quantum-L9/.github `semantics/conformance_model.yaml`
`l9_conformance_owns`): fixture identity, fixture admission, profile resolution,
executable conformance, conformance receipts. This repository never creates global
law, never admits a subject, and never treats a receipt as authority.

## Commands

| Command | Purpose |
|---|---|
| `make sync` | install the locked environment (`uv.lock`) |
| `make lint` | ruff format check + ruff lint |
| `make typecheck` | pyright strict |
| `make test` | pytest |
| `make check` | catalog/profile witnesses |
| `make ci` | all of the above |

## Rules for agents

- A fixture or profile must trace to existing governing semantics; record an Unknown
  instead of authoring law.
- The system under test never defines the expected result.
- The runner verifies every bound digest before executing peer-supplied code, and runs
  each case in a fresh temporary directory and process.
- Verdicts are `pass | fail | error | unresolved`; anything not proven passing is not `pass`.
- Receipts (`schemas/conformance-receipt-1.schema.json`) are immutable and content-addressed;
  never add a mutable status/currentness field or an admission field.
- Published profile and fixture bytes are immutable: change means a new `@N` version and a
  new `SHA256SUMS` line.
