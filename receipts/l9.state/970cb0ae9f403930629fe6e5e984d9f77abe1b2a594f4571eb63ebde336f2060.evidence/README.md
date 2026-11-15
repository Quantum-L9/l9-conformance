# M2 trial receipt — l9.state (candidate-authority rehearsal)

Receipt: `../970cb0ae9f403930629fe6e5e984d9f77abe1b2a594f4571eb63ebde336f2060.json`
(`l9-conformance/receipt:sha256:970cb0ae…f2060`, grade `candidate_authority_rehearsal`,
overall verdict `unresolved`). Evidence only: it admits, authorizes and publishes nothing.

## Inputs (all pinned)

| Input | Pin |
|---|---|
| Subject | `Quantum-L9/l9-wip@711da6842378eea88d5dace26ffa48bedd632a7b`, `l9-state/l9-state-FULL-WORK-PACK-HANDOFF-READY/l9-state` |
| Authority (not admitted) | `Quantum-L9/.github@44e02a090ea0e27f91c8fe3c245e2033ad80699f` |
| Compiler | `Quantum-L9/l9-semantic-compiler-engine@9040ff9e70033ffeb56fbb44dea678253f62ecb0` (tree `c4a1800929065b42bc363b1f45d20a77b5411e40`) |
| Compiler tools run | `tools/rehearse_product.py` sha256 `af3e54ab…fdb29`; package `src/l9_semantic_compiler` at that commit (`tools/run_conformance_suite.py` sha256 `eee535bd…55270` verified, not run: this runner executes) |
| Rehearsal venv | `venv-receipt.json` (binds `requirements.lock` sha256 `a5c09355…21d67` and lists every installed version; the hashed lock is kept outside the repository and available on request), `requirements.in` — Python 3.13.18, Gate_SDK@bc1c74c0, pytest 9.1.1 |

## How it was produced

1. `compile_stages.py` ran the compiler's stages 1–6 on its specimen
   `tests/specimens/l9-state-711da68` without execution. It records the return value of
   the compiler's own `lower_fixture_ir` so the fixture IR bytes can be bound; nothing
   else is altered.
2. `scripts/rehearse.py --plan rehearsal-plan.yaml` fetched the subject and authority at
   their pins (clean, detached), bound the harness root as declared in the subject's
   `repository-spec.yaml`, bound the three product files the harness driver reads
   (`product-topology.yaml`, `contracts/STATE_HARNESS_SURFACE.yaml`,
   `contracts/STATE_HARNESS_CONTRACTS.yaml`), staged `src/l9_state` as the subject
   (`subject-manifest.yaml`), and ran every executable obligation's cases in a fresh
   directory and process.

## Regenerating the bound IRs

The conformance IR and fixture IR are not stored here; the receipt binds both. Running
`compile_stages.py <compiler checkout at 9040ff9> <out>` with PyYAML and jsonschema
regenerates them: `conformance_ir.json` is the compiler's own output file (raw
`f767c515…792b`), and `fixture_ir.json` is `json.dumps(sort_keys=True, indent=1)` of the
captured value (raw `3d320bd9…f71c`, semantic `b7f05520…303e`).
`l9_conformance.receipt.verify_receipt_inputs` checks both against the receipt.

## Cross-checks

* Compiler outputs equal COMPILER's reference values: conformance IR file
  `f767c515…792b`, semantic `2be9d557…b479`; fixture IR semantic `b7f05520…303e`;
  closure `7de97e42…9585`; all four suite files.
* The compiler specimen's 72 git blobs equal the subject at `711da68` (0 mismatches).

## Result and deviations (findings, not reconciled)

* Obligations: 24 pass, 7 unresolved, 0 fail, 0 error (COMPILER: 24 / 7 / 0 / 0).
* Cases: 70 distinct cases executed (131 case runs across obligations), all pass.
  COMPILER reports 73 pass / 1 unresolved over the suite's 74 cases. The 4 cases this
  runner did not execute witness only unresolved obligations, which it never executes:
  `state.create.operation_collision_has_no_effect`,
  `state.create.transport_idempotency_key_must_match_operation`,
  `state.get.missing_object_is_problem`, `state.history.missing_object_is_problem`.
* The harness reads product files at runtime that `conformance_harness_roots` does not
  declare; they are bound here as harness data files.
