"""Receipt schema l9-conformance/conformance-receipt@1: positive and falsifying cases."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest
from conftest import Closure, ObligationSpec, replay

from l9_conformance.receipt import (
    IDENTITY_PREFIX,
    ReceiptInvalid,
    body_digest,
    finalize,
    schema_errors,
    verify_receipt,
)
from l9_conformance.runner import run

Build = Callable[[list[ObligationSpec]], Closure]


@pytest.fixture
def receipt(make_closure: Build) -> dict[str, Any]:
    return run(make_closure([replay("ob/a", "test_pass"), replay("ob/b", "test_fail")]).request())


def _reseal(r: dict[str, Any]) -> dict[str, Any]:
    body = {k: v for k, v in r.items() if k not in ("receipt_identity", "receipt_digest")}
    digest = body_digest(body)
    return {**body, "receipt_digest": digest, "receipt_identity": IDENTITY_PREFIX + digest}


def test_emitted_receipt_is_valid_and_content_addressed(receipt: dict[str, Any]) -> None:
    assert schema_errors(receipt) == []
    verify_receipt(receipt)
    assert receipt["receipt_identity"] == IDENTITY_PREFIX + receipt["receipt_digest"]
    assert receipt["receipt_digest"] == body_digest(receipt)


def test_identity_is_deterministic_for_identical_body(receipt: dict[str, Any]) -> None:
    body = {k: v for k, v in receipt.items() if k not in ("receipt_identity", "receipt_digest")}
    assert finalize(copy.deepcopy(body))["receipt_identity"] == receipt["receipt_identity"]


def test_finalize_refuses_preset_identity(receipt: dict[str, Any]) -> None:
    with pytest.raises(ReceiptInvalid, match="pre-set"):
        finalize(receipt)


def test_tampered_content_fails_digest(receipt: dict[str, Any]) -> None:
    tampered = copy.deepcopy(receipt)
    tampered["exact_subject_ref"]["product_ref"] = "synthetic://other"
    with pytest.raises(ReceiptInvalid, match="receipt_digest mismatch"):
        verify_receipt(tampered)


def test_identity_not_derived_from_digest_fails(receipt: dict[str, Any]) -> None:
    tampered = copy.deepcopy(receipt)
    tampered["receipt_identity"] = IDENTITY_PREFIX + "sha256:" + "0" * 64
    with pytest.raises(ReceiptInvalid, match="receipt_identity"):
        verify_receipt(tampered)


def _forge(receipt: dict[str, Any], mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    forged = copy.deepcopy(receipt)
    mutate(forged)
    return _reseal(forged)


def _outcome(r: dict[str, Any]) -> dict[str, Any]:
    return r["observation_or_outcome"]


FALSIFYING: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("mutable status field", lambda r: r.update(status="current"), "schema"),
    ("currentness field", lambda r: r.update(currentness="current"), "schema"),
    ("admission decision field", lambda r: r.update(admission_decision="admitted"), "schema"),
    ("is_admission true", lambda r: r["authority"].update(is_admission=True), "schema"),
    (
        "grants authority",
        lambda r: r["authority"].update(grants_execution_authority=True),
        "schema",
    ),
    (
        "verdict outside closed set",
        lambda r: _outcome(r).update(overall_verdict="mostly"),
        "schema",
    ),
    ("missing producer", lambda r: r.pop("producer_identity"), "schema"),
    ("wrong receipt type", lambda r: r.update(receipt_type="l9.receipt/admission@1"), "schema"),
    (
        "short revision",
        lambda r: r["governing_coordinates"]["canonical_authority"].update(revision="abc"),
        "schema",
    ),
    (
        "overall pass while an obligation fails",
        lambda r: _outcome(r).update(overall_verdict="pass"),
        "overall_verdict",
    ),
    (
        "obligation pass while a case fails",
        lambda r: _outcome(r)["obligations"][1].update(verdict="pass"),
        "derived",
    ),
    (
        "non-replay obligation recorded as pass",
        lambda r: _outcome(r)["obligations"][0].update(verification_class="static_inspection"),
        "cannot pass",
    ),
    (
        "canonical grade on unadmitted authority",
        lambda r: r.update(evidence_grade="canonical"),
        "admitted",
    ),
]


@pytest.mark.parametrize(("label", "mutate", "match"), FALSIFYING, ids=[f[0] for f in FALSIFYING])
def test_falsifying_receipts_rejected(
    receipt: dict[str, Any], label: str, mutate: Callable[[dict[str, Any]], None], match: str
) -> None:
    with pytest.raises(ReceiptInvalid, match=match):
        verify_receipt(_forge(receipt, mutate))


def test_empty_obligations_cannot_claim_pass(receipt: dict[str, Any]) -> None:
    def mutate(r: dict[str, Any]) -> None:
        _outcome(r)["obligations"] = []
        _outcome(r)["overall_verdict"] = "pass"

    with pytest.raises(ReceiptInvalid, match="overall_verdict"):
        verify_receipt(_forge(receipt, mutate))


def test_rendered_suite_digest_must_match_file_list(receipt: dict[str, Any]) -> None:
    def mutate(r: dict[str, Any]) -> None:
        r["governing_coordinates"]["rendered_suite"]["files"][0]["sha256"] = "sha256:" + "d" * 64

    with pytest.raises(ReceiptInvalid, match=r"rendered_suite\.digest"):
        verify_receipt(_forge(receipt, mutate))
