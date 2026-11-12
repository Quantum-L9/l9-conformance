"""Receipt schema ``l9-conformance/conformance-receipt@1``.

Specializes ``l9.receipt/conformance@1`` (Quantum-L9/.github
semantics/receipt_catalog.yaml). A receipt is immutable evidence:

* its identity is content-addressed (``receipt_digest`` over the canonical body),
* it carries no mutable currentness status (currentness is derived by evaluating
  ``governing_coordinates``; lifecycle.yaml ``conformance_receipt``),
* it is never admission and grants no authority (schema ``authority`` consts).

Verification never relies only on the recorded verdict: ``verify_receipt``
recomputes the digest and re-derives every obligation verdict and the overall
verdict from the recorded case outcomes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from functools import cache
from pathlib import Path
from typing import Any, Final, Literal, cast

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from l9_conformance.digests import file_set_digest, sha256_canonical

SCHEMA_ID: Final = "l9-conformance/conformance-receipt@1"
RECEIPT_TYPE: Final = "l9.receipt/conformance@1"
IDENTITY_PREFIX: Final = "l9-conformance/receipt:"
CLAIM: Final = (
    "The exact subject was executed against the exact bound conformance closure and produced "
    "the recorded per-obligation outcomes. This receipt does not admit, authorize, or publish "
    "the subject."
)
AUTHORITY: Final = {
    "grants_semantic_authority": False,
    "grants_execution_authority": False,
    "is_admission": False,
}
SCHEMA_PATH: Final = (
    Path(__file__).resolve().parents[1] / "schemas" / "conformance-receipt-1.schema.json"
)

Verdict = Literal["pass", "fail", "error", "unresolved"]
VERDICTS: Final[tuple[Verdict, ...]] = ("pass", "fail", "error", "unresolved")
# Non-pass precedence when aggregating: a definite failure outranks an
# execution error, which outranks an unresolved outcome.
_NON_PASS_ORDER: Final[tuple[Verdict, ...]] = ("fail", "error", "unresolved")


class ReceiptInvalid(ValueError):
    """Raised when a receipt fails schema validation or independent verification."""


@cache
def load_schema() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def _validator() -> Draft202012Validator:
    return Draft202012Validator(load_schema(), format_checker=FormatChecker())


def schema_errors(receipt: Mapping[str, Any]) -> list[str]:
    validator = _validator()
    raw_errors: list[ValidationError] = list(
        validator.iter_errors(cast(Any, receipt))  # pyright: ignore[reportUnknownMemberType]
    )
    errors = sorted(raw_errors, key=lambda e: [str(p) for p in e.absolute_path])
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors]


def aggregate(verdicts: Iterable[Verdict]) -> Verdict:
    """``pass`` only when non-empty and every member passed. Empty is unresolved."""
    seen = list(verdicts)
    if not seen:
        return "unresolved"
    if all(v == "pass" for v in seen):
        return "pass"
    for candidate in _NON_PASS_ORDER:
        if candidate in seen:
            return candidate
    return "unresolved"


def overall_from_obligations(obligations: Iterable[Mapping[str, Any]]) -> Verdict:
    """Overall verdict over *required* obligations; no required obligation -> unresolved."""
    required: list[Verdict] = [cast(Verdict, o["verdict"]) for o in obligations if o["required"]]
    return aggregate(required)


def body_digest(receipt: Mapping[str, Any]) -> str:
    body = {k: v for k, v in receipt.items() if k not in ("receipt_identity", "receipt_digest")}
    return sha256_canonical(body)


def finalize(body: Mapping[str, Any]) -> dict[str, Any]:
    """Seal a receipt body: compute digest and content-addressed identity, then validate."""
    if "receipt_identity" in body or "receipt_digest" in body:
        raise ReceiptInvalid("receipt body must not pre-set identity or digest")
    receipt = dict(body)
    digest = body_digest(receipt)
    receipt["receipt_digest"] = digest
    receipt["receipt_identity"] = IDENTITY_PREFIX + digest
    verify_receipt(receipt)
    return receipt


def verify_receipt(receipt: Mapping[str, Any]) -> None:
    """Schema-validate and independently re-derive the receipt's verdicts and digest."""
    errors = schema_errors(receipt)
    if errors:
        raise ReceiptInvalid("schema: " + "; ".join(errors))
    digest = body_digest(receipt)
    if receipt["receipt_digest"] != digest:
        raise ReceiptInvalid(
            f"receipt_digest mismatch: recorded {receipt['receipt_digest']}, computed {digest}"
        )
    if receipt["receipt_identity"] != IDENTITY_PREFIX + digest:
        raise ReceiptInvalid("receipt_identity is not derived from receipt_digest")
    outcome = cast(Mapping[str, Any], receipt["observation_or_outcome"])
    obligations = cast(list[Mapping[str, Any]], outcome["obligations"])
    for obligation in obligations:
        cases = cast(list[Mapping[str, Any]], obligation["cases"])
        if obligation["verification_class"] == "fixture_replay":
            derived = aggregate(cast(Verdict, c["verdict"]) for c in cases)
            if obligation["verdict"] != derived:
                raise ReceiptInvalid(
                    f"obligation {obligation['obligation_id']}: recorded {obligation['verdict']}, "
                    f"derived {derived} from cases"
                )
        elif obligation["verdict"] == "pass":
            # The runner executes no non-replay verification class; it cannot pass here.
            raise ReceiptInvalid(
                f"obligation {obligation['obligation_id']}: {obligation['verification_class']} "
                "cannot pass without executed evidence"
            )
    derived_overall = overall_from_obligations(obligations)
    if outcome["overall_verdict"] != derived_overall:
        raise ReceiptInvalid(
            f"overall_verdict recorded {outcome['overall_verdict']}, derived {derived_overall}"
        )
    coordinates = cast(Mapping[str, Any], receipt["governing_coordinates"])
    for block in ("rendered_suite", "harness"):
        if block in coordinates:
            bound = cast(Mapping[str, Any], coordinates[block])
            files = cast(list[dict[str, str]], bound["files"])
            if file_set_digest(files) != bound["digest"]:
                raise ReceiptInvalid(f"{block}.digest is not the digest of its file list")
    authority = cast(Mapping[str, Any], coordinates["canonical_authority"])
    if receipt["evidence_grade"] == "canonical" and not authority["admitted"]:
        raise ReceiptInvalid("canonical evidence_grade requires admitted canonical authority")
