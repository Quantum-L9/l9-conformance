"""Run COMPILER's authorized rehearse_product stages 1-6 (no execution) and keep the fixture IR.

The compiler's rehearsal writes conformance_ir.json and suite/ but keeps the fixture IR in memory.
This wrapper records the return value of the compiler's own lower_fixture_ir call (unchanged) so
the runner can bind the fixture IR bytes. Nothing else is altered.
"""

import hashlib
import json
import sys
from pathlib import Path

compiler, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
expected = {
    "tools/rehearse_product.py": "af3e54ab63be13a344d6be24d7d62ba7185c8b010bc8315b776ec44380fbdb29",
    "tools/run_conformance_suite.py": "eee535bd52391b92de9717be616497312f32b5add43a68a0d3d7abc526855270",
}
for rel, want in expected.items():
    got = hashlib.sha256((compiler / rel).read_bytes()).hexdigest()
    if got != want:
        raise SystemExit(f"REFUSED: {rel} sha256 {got} != authorized {want}")
sys.path.insert(0, str(compiler / "src"))
sys.path.insert(0, str(compiler / "tools"))
import rehearse_product  # noqa: E402
from l9_semantic_compiler.product import rehearsal  # noqa: E402

captured = {}
_orig = rehearsal.lower_fixture_ir


def _capture(*a, **k):
    result = _orig(*a, **k)
    captured["fixture_ir"] = result.fixture_ir
    return result


rehearsal.lower_fixture_ir = _capture

doc = rehearse_product.rehearse_specimen(
    compiler / "tests/specimens/l9-state-711da68",
    compiler_root=compiler,
    schemas_dir=compiler / "tests/specimens/github-semantics-f1-v3.40.0/semantics",
    authority_revision="Quantum-L9/.github@44e02a090ea0e27f91c8fe3c245e2033ad80699f",
    driver="l9_state_conformance_harness.driver:create_driver",
    out=out,
)
if captured.get("fixture_ir") is not None:
    (out / "fixture_ir.json").write_text(
        json.dumps(captured["fixture_ir"], sort_keys=True, indent=1)
    )
print(doc["status"], json.dumps(doc["refusals"])[:2000])
print(json.dumps(doc["digests"], indent=1, sort_keys=True)[:4000])
