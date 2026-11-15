# Compiled closure fixture: l9.state @ 711da68

Real compiler output, never hand-edited. Tests copy it into a temporary directory before
mutating anything.

| Field | Value |
|---|---|
| Producer | `Quantum-L9/l9-semantic-compiler-engine` `57f91c0` (0.16.0, `birth-closure`, ADR-0007) |
| Product | the compiler's A10 State specimen `tests/specimens/l9-state-711da68`, prepared by its `tests/test_birth_closure.py::_state` (modelled Gate registration coordinate, declared driver `l9_state_conformance_harness.driver:create_driver`) |
| `closure/` | exactly `<out>/conformance/` (`closure.json` `l9.conformance-closure/v1`, conformance IR, fixture IR, pytest/v2 suite, node artifact) |
| `product/` | only the product's declared conformance harness root (`src/l9_state_conformance_harness`), the driver the closure binds by digest |
| Schemas | `Quantum-L9/.github` v3.40.0 candidates (`authority_status: candidate_not_admitted`) |

Regenerate or verify (interpreter with the compiler checkout installed):

```bash
python scripts/regen_compiled_closure.py --compiler-src <l9-semantic-compiler-engine> --check
```

Known content (evidence, not a defect of this repository):

- the node artifact does not contain `l9_state/retention.py`, which the harness imports, so
  every executable case of a run over this closure is `error` (ModuleNotFoundError). The
  same suite against the product source passes 73 cases and leaves 1 unresolved;
- the State driver also reads product data files (its topology and harness contracts) from
  the product tree, which the closure does not declare; under ruling D5 only the harness
  root is laid out, so those reads would fail too once the import succeeds.
