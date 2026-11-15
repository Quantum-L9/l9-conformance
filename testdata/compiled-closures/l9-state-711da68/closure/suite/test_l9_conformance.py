"""Rendered by l9-semantic-compiler (pytest/v2): one test function per fixture case."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import L9EvaluationError, L9Unresolved, SetupError
from l9_conformance_eval import evaluate_assertion, evaluate_precondition, resolve_path, _MISSING

_DATA = json.loads((Path(__file__).parent / "l9_conformance_cases.json").read_text(encoding="utf-8"))
_CASES = {case["id"]: case for case in _DATA["cases"]}
_CONTROLS = set(_DATA["harness_controls"])
_OBSERVATIONS = set(_DATA["harness_observations"])


def _resolve(value, recorded):
    """Replace every {"$from": {step, path}} with the recorded value; absent is a setup error."""
    if isinstance(value, dict):
        if len(value) == 1 and next(iter(value)) == "$from":
            ref = value["$from"]
            source = recorded.get(ref.get("step"), _MISSING)
            found = _MISSING if source is _MISSING else resolve_path(source, ref.get("path", ""))
            if found is _MISSING:
                raise SetupError(f"$from {ref!r} names no recorded value")
            return found
        return {key: _resolve(child, recorded) for key, child in value.items()}
    if isinstance(value, list):
        return [_resolve(child, recorded) for child in value]
    return value


def _call(driver, operation, request, context, contracts, step_id):
    result = driver.interface_action(operation, request, context)
    contracts[step_id] = driver.result_contract(operation, result)
    return result


def _given(driver, case, recorded, contracts):
    for step in case["given"]["steps"]:
        request = _resolve(step.get("request"), recorded)
        kind = step["kind"]
        if kind == "interface_action":
            result = _call(driver, step["operation"], request, step.get("context"), contracts, step["id"])
        elif kind == "port_fixture":
            result = driver.port_fixture(step["operation"], request)
        elif kind == "harness_control":
            if step["operation"] not in _CONTROLS:
                raise SetupError(f"undeclared harness control {step['operation']}")
            result = driver.harness_control(step["operation"], request)
            contracts[step["id"]] = driver.result_contract(step["operation"], result)
        else:
            raise SetupError(f"unknown given step kind {kind}")
        recorded[step["id"]] = result
        if "precondition" in step and evaluate_precondition(step["precondition"], result) != "hold":
            raise SetupError(f"precondition of {step['id']} does not hold")


def _when(driver, case, recorded, contracts):
    when = case["when"]
    if when["mode"] == "single":
        steps = [{"id": "when", "operation": when["operation"], "request": when["request"], **({"context": when["context"]} if "context" in when else {})}]
    else:
        steps = when["steps"]

    def issue(step, visible):
        return _call(driver, step["operation"], _resolve(step.get("request"), visible), step.get("context"), contracts, step["id"])

    if when["mode"] == "parallel":
        visible = dict(recorded)
        with ThreadPoolExecutor(max_workers=len(steps)) as pool:
            futures = {step["id"]: pool.submit(issue, step, visible) for step in steps}
        for step_id, future in futures.items():
            recorded[step_id] = future.result()
    else:
        for step in steps:
            recorded[step["id"]] = issue(step, recorded)
    return [recorded[step["id"]] for step in steps]


def _observe(driver, case, recorded, contracts):
    for obs in case["expect"]["observations"]:
        request = _resolve(obs.get("request"), recorded)
        if obs["kind"] == "interface_action":
            recorded[obs["id"]] = _call(driver, obs["operation"], request, obs.get("context"), contracts, obs["id"])
        elif obs["operation"] in _OBSERVATIONS:
            recorded[obs["id"]] = driver.harness_observation(obs["operation"], request)
            contracts[obs["id"]] = driver.result_contract(obs["operation"], recorded[obs["id"]])
        else:
            raise SetupError(f"undeclared harness observation {obs['operation']}")


@pytest.fixture
def l9_run(request):
    case = _CASES[request.function.l9_case]
    if case["disposition"] != "executable":
        return None
    driver = request.getfixturevalue("l9_driver")
    driver.fresh_subject()
    recorded, contracts = {}, {}
    _given(driver, case, recorded, contracts)
    group = _when(driver, case, recorded, contracts)
    _observe(driver, case, recorded, contracts)
    return recorded, group, contracts


def _check(case_id, run):
    case = _CASES[case_id]
    if case["disposition"] != "executable":
        raise L9Unresolved(f"{case_id}: {case.get('blocked_by')}")
    recorded, group, contracts = run
    outcomes = {}
    for assertion in case["expect"]["assertions"]:
        if assertion["predicate"] == "result_contract_is":
            ref = assertion["target"].get("ref")
            if assertion["target"]["kind"] == "group" or ref not in contracts:
                result = "error"
            else:
                result = "pass" if contracts[ref] == assertion["operand"] else "fail"
        else:
            result = evaluate_assertion(assertion, recorded, group)
        outcomes[assertion["id"]] = result
    if "error" in outcomes.values():
        raise L9EvaluationError(f"{case_id}: {outcomes}")
    # Explicit raise, never `assert`: python -O / PYTHONOPTIMIZE must not turn a fail into a pass.
    if any(o != "pass" for o in outcomes.values()):
        raise AssertionError(outcomes)


def _case(case_id):
    def mark(function):
        function.l9_case = case_id
        return function

    return mark


@_case('state.create.commits_revision_zero')
def test_state_create_commits_revision_zero(l9_run):
    _check('state.create.commits_revision_zero', l9_run)


@_case('state.create.exact_replay_returns_original_receipt')
def test_state_create_exact_replay_returns_original_receipt(l9_run):
    _check('state.create.exact_replay_returns_original_receipt', l9_run)


@_case('state.create.operation_collision_has_no_effect')
def test_state_create_operation_collision_has_no_effect(l9_run):
    _check('state.create.operation_collision_has_no_effect', l9_run)


@_case('state.create.existing_object_is_sticky_conflict')
def test_state_create_existing_object_is_sticky_conflict(l9_run):
    _check('state.create.existing_object_is_sticky_conflict', l9_run)


@_case('state.create.unadmitted_schema_is_refused')
def test_state_create_unadmitted_schema_is_refused(l9_run):
    _check('state.create.unadmitted_schema_is_refused', l9_run)


@_case('state.create.schema_digest_mismatch_is_refused')
def test_state_create_schema_digest_mismatch_is_refused(l9_run):
    _check('state.create.schema_digest_mismatch_is_refused', l9_run)


@_case('state.create.unadmitted_retention_is_refused')
def test_state_create_unadmitted_retention_is_refused(l9_run):
    _check('state.create.unadmitted_retention_is_refused', l9_run)


@_case('state.create.malformed_request_is_nondurable_problem')
def test_state_create_malformed_request_is_nondurable_problem(l9_run):
    _check('state.create.malformed_request_is_nondurable_problem', l9_run)


@_case('state.create.transport_idempotency_key_must_match_operation')
def test_state_create_transport_idempotency_key_must_match_operation(l9_run):
    _check('state.create.transport_idempotency_key_must_match_operation', l9_run)


@_case('state.create.precommit_deadline_has_no_durable_receipt')
def test_state_create_precommit_deadline_has_no_durable_receipt(l9_run):
    _check('state.create.precommit_deadline_has_no_durable_receipt', l9_run)


@_case('state.create.storage_unavailable_precommit_is_same_operation_retry')
def test_state_create_storage_unavailable_precommit_is_same_operation_retry(l9_run):
    _check('state.create.storage_unavailable_precommit_is_same_operation_retry', l9_run)


@_case('state.create.ambiguous_commit_reconciles_by_receipt')
def test_state_create_ambiguous_commit_reconciles_by_receipt(l9_run):
    _check('state.create.ambiguous_commit_reconciles_by_receipt', l9_run)


@_case('state.create.tenants_are_isolated')
def test_state_create_tenants_are_isolated(l9_run):
    _check('state.create.tenants_are_isolated', l9_run)


@_case('state.get.current_and_named_revisions')
def test_state_get_current_and_named_revisions(l9_run):
    _check('state.get.current_and_named_revisions', l9_run)


@_case('state.get.missing_object_is_problem')
def test_state_get_missing_object_is_problem(l9_run):
    _check('state.get.missing_object_is_problem', l9_run)


@_case('state.get.storage_unavailable_read_is_retry_later')
def test_state_get_storage_unavailable_read_is_retry_later(l9_run):
    _check('state.get.storage_unavailable_read_is_retry_later', l9_run)


@_case('state.read.observations_create_no_receipt')
def test_state_read_observations_create_no_receipt(l9_run):
    _check('state.read.observations_create_no_receipt', l9_run)


@_case('state.transition.commits_next_revision_with_event')
def test_state_transition_commits_next_revision_with_event(l9_run):
    _check('state.transition.commits_next_revision_with_event', l9_run)


@_case('state.transition.stale_revision_is_sticky_conflict')
def test_state_transition_stale_revision_is_sticky_conflict(l9_run):
    _check('state.transition.stale_revision_is_sticky_conflict', l9_run)


@_case('state.transition.changed_world_retry_needs_new_operation')
def test_state_transition_changed_world_retry_needs_new_operation(l9_run):
    _check('state.transition.changed_world_retry_needs_new_operation', l9_run)


@_case('state.transition.state_digest_conflict')
def test_state_transition_state_digest_conflict(l9_run):
    _check('state.transition.state_digest_conflict', l9_run)


@_case('state.transition.concurrent_cas_writers_exactly_one_commits')
def test_state_transition_concurrent_cas_writers_exactly_one_commits(l9_run):
    _check('state.transition.concurrent_cas_writers_exactly_one_commits', l9_run)


@_case('state.transition.schema_binding_is_immutable_and_refusal_sticky')
def test_state_transition_schema_binding_is_immutable_and_refusal_sticky(l9_run):
    _check('state.transition.schema_binding_is_immutable_and_refusal_sticky', l9_run)


@_case('state.transition.missing_object_is_durable_conflict')
def test_state_transition_missing_object_is_durable_conflict(l9_run):
    _check('state.transition.missing_object_is_durable_conflict', l9_run)


@_case('state.transition.tombstoned_object_is_refused')
def test_state_transition_tombstoned_object_is_refused(l9_run):
    _check('state.transition.tombstoned_object_is_refused', l9_run)


@_case('state.claim.acquires_first_fence')
def test_state_claim_acquires_first_fence(l9_run):
    _check('state.claim.acquires_first_fence', l9_run)


@_case('state.claim.active_claim_requires_exact_fence')
def test_state_claim_active_claim_requires_exact_fence(l9_run):
    _check('state.claim.active_claim_requires_exact_fence', l9_run)


@_case('state.claim.foreign_holder_is_refused')
def test_state_claim_foreign_holder_is_refused(l9_run):
    _check('state.claim.foreign_holder_is_refused', l9_run)


@_case('state.claim.second_claim_while_active_conflicts')
def test_state_claim_second_claim_while_active_conflicts(l9_run):
    _check('state.claim.second_claim_while_active_conflicts', l9_run)


@_case('state.claim.stale_expected_revision_conflicts')
def test_state_claim_stale_expected_revision_conflicts(l9_run):
    _check('state.claim.stale_expected_revision_conflicts', l9_run)


@_case('state.claim.missing_object_conflicts')
def test_state_claim_missing_object_conflicts(l9_run):
    _check('state.claim.missing_object_conflicts', l9_run)


@_case('state.claim.lease_reassignment_increments_fence')
def test_state_claim_lease_reassignment_increments_fence(l9_run):
    _check('state.claim.lease_reassignment_increments_fence', l9_run)


@_case('state.claim.expired_claim_cannot_authorize_writes')
def test_state_claim_expired_claim_cannot_authorize_writes(l9_run):
    _check('state.claim.expired_claim_cannot_authorize_writes', l9_run)


@_case('state.claim.holder_without_scope_authorization_is_refused')
def test_state_claim_holder_without_scope_authorization_is_refused(l9_run):
    _check('state.claim.holder_without_scope_authorization_is_refused', l9_run)


@_case('state.renew.extends_lease_with_same_fence')
def test_state_renew_extends_lease_with_same_fence(l9_run):
    _check('state.renew.extends_lease_with_same_fence', l9_run)


@_case('state.renew.stale_fence_conflicts')
def test_state_renew_stale_fence_conflicts(l9_run):
    _check('state.renew.stale_fence_conflicts', l9_run)


@_case('state.renew.expired_claim_conflicts')
def test_state_renew_expired_claim_conflicts(l9_run):
    _check('state.renew.expired_claim_conflicts', l9_run)


@_case('state.release.ends_claim_and_fence_stays_monotonic')
def test_state_release_ends_claim_and_fence_stays_monotonic(l9_run):
    _check('state.release.ends_claim_and_fence_stays_monotonic', l9_run)


@_case('state.release.foreign_holder_is_refused')
def test_state_release_foreign_holder_is_refused(l9_run):
    _check('state.release.foreign_holder_is_refused', l9_run)


@_case('state.release.expired_claim_conflicts')
def test_state_release_expired_claim_conflicts(l9_run):
    _check('state.release.expired_claim_conflicts', l9_run)


@_case('state.tombstone.suppresses_current_read_only')
def test_state_tombstone_suppresses_current_read_only(l9_run):
    _check('state.tombstone.suppresses_current_read_only', l9_run)


@_case('state.tombstone.already_tombstoned_is_refused')
def test_state_tombstone_already_tombstoned_is_refused(l9_run):
    _check('state.tombstone.already_tombstoned_is_refused', l9_run)


@_case('state.tombstone.object_identity_is_not_reusable')
def test_state_tombstone_object_identity_is_not_reusable(l9_run):
    _check('state.tombstone.object_identity_is_not_reusable', l9_run)


@_case('state.restore.creates_new_active_revision')
def test_state_restore_creates_new_active_revision(l9_run):
    _check('state.restore.creates_new_active_revision', l9_run)


@_case('state.restore.active_object_conflicts')
def test_state_restore_active_object_conflicts(l9_run):
    _check('state.restore.active_object_conflicts', l9_run)


@_case('state.restore.source_must_be_retained_prior_revision')
def test_state_restore_source_must_be_retained_prior_revision(l9_run):
    _check('state.restore.source_must_be_retained_prior_revision', l9_run)


@_case('state.erase.scrubs_payload_preserves_identity_and_replay')
def test_state_erase_scrubs_payload_preserves_identity_and_replay(l9_run):
    _check('state.erase.scrubs_payload_preserves_identity_and_replay', l9_run)


@_case('state.erase.is_not_a_gate_action')
def test_state_erase_is_not_a_gate_action(l9_run):
    _check('state.erase.is_not_a_gate_action', l9_run)


@_case('state.list.then_events_cold_resume')
def test_state_list_then_events_cold_resume(l9_run):
    _check('state.list.then_events_cold_resume', l9_run)


@_case('state.list.cursor_filter_must_match')
def test_state_list_cursor_filter_must_match(l9_run):
    _check('state.list.cursor_filter_must_match', l9_run)


@_case('state.events.total_order_within_scope')
def test_state_events_total_order_within_scope(l9_run):
    _check('state.events.total_order_within_scope', l9_run)


@_case('state.events.bounded_paging_always_returns_cursor')
def test_state_events_bounded_paging_always_returns_cursor(l9_run):
    _check('state.events.bounded_paging_always_returns_cursor', l9_run)


@_case('state.events.cursor_is_scope_bound')
def test_state_events_cursor_is_scope_bound(l9_run):
    _check('state.events.cursor_is_scope_bound', l9_run)


@_case('state.events.cursor_is_consumer_bound')
def test_state_events_cursor_is_consumer_bound(l9_run):
    _check('state.events.cursor_is_consumer_bound', l9_run)


@_case('state.events.tampered_cursor_is_invalid')
def test_state_events_tampered_cursor_is_invalid(l9_run):
    _check('state.events.tampered_cursor_is_invalid', l9_run)


@_case('state.events.foreign_consumer_requires_authorization')
def test_state_events_foreign_consumer_requires_authorization(l9_run):
    _check('state.events.foreign_consumer_requires_authorization', l9_run)


@_case('state.ack.commits_checkpoint_used_for_resume')
def test_state_ack_commits_checkpoint_used_for_resume(l9_run):
    _check('state.ack.commits_checkpoint_used_for_resume', l9_run)


@_case('state.history.bounded_paging')
def test_state_history_bounded_paging(l9_run):
    _check('state.history.bounded_paging', l9_run)


@_case('state.history.retention_gap_is_history_unavailable')
def test_state_history_retention_gap_is_history_unavailable(l9_run):
    _check('state.history.retention_gap_is_history_unavailable', l9_run)


@_case('state.history.missing_object_is_problem')
def test_state_history_missing_object_is_problem(l9_run):
    _check('state.history.missing_object_is_problem', l9_run)


@_case('state.operation.inspect.reports_found_and_not_found')
def test_state_operation_inspect_reports_found_and_not_found(l9_run):
    _check('state.operation.inspect.reports_found_and_not_found', l9_run)


@_case('state.operation.inspect.authorization_precedes_replay')
def test_state_operation_inspect_authorization_precedes_replay(l9_run):
    _check('state.operation.inspect.authorization_precedes_replay', l9_run)


@_case('state.operation.inspect.cross_principal_visibility')
def test_state_operation_inspect_cross_principal_visibility(l9_run):
    _check('state.operation.inspect.cross_principal_visibility', l9_run)


@_case('state.events.retention_gap_requires_cold_resync')
def test_state_events_retention_gap_requires_cold_resync(l9_run):
    _check('state.events.retention_gap_requires_cold_resync', l9_run)


@_case('state.events.new_consumer_starts_at_retained_floor')
def test_state_events_new_consumer_starts_at_retained_floor(l9_run):
    _check('state.events.new_consumer_starts_at_retained_floor', l9_run)


@_case('state.create.schema_rejected_payload_is_durable_refusal')
def test_state_create_schema_rejected_payload_is_durable_refusal(l9_run):
    _check('state.create.schema_rejected_payload_is_durable_refusal', l9_run)


@_case('state.transition.schema_rejected_payload_is_durable_refusal')
def test_state_transition_schema_rejected_payload_is_durable_refusal(l9_run):
    _check('state.transition.schema_rejected_payload_is_durable_refusal', l9_run)


@_case('state.ack.regression_is_nondurable_problem')
def test_state_ack_regression_is_nondurable_problem(l9_run):
    _check('state.ack.regression_is_nondurable_problem', l9_run)


@_case('state.claim.tombstoned_object_is_claimable_and_fences_restore')
def test_state_claim_tombstoned_object_is_claimable_and_fences_restore(l9_run):
    _check('state.claim.tombstoned_object_is_claimable_and_fences_restore', l9_run)


@_case('state.get.read_deadline_is_retry_later')
def test_state_get_read_deadline_is_retry_later(l9_run):
    _check('state.get.read_deadline_is_retry_later', l9_run)


@_case('state.list.read_deadline_is_retry_later')
def test_state_list_read_deadline_is_retry_later(l9_run):
    _check('state.list.read_deadline_is_retry_later', l9_run)


@_case('state.events.read_deadline_is_retry_later')
def test_state_events_read_deadline_is_retry_later(l9_run):
    _check('state.events.read_deadline_is_retry_later', l9_run)


@_case('state.history.read_deadline_is_retry_later')
def test_state_history_read_deadline_is_retry_later(l9_run):
    _check('state.history.read_deadline_is_retry_later', l9_run)


@_case('state.operation.inspect.read_deadline_is_retry_later')
def test_state_operation_inspect_read_deadline_is_retry_later(l9_run):
    _check('state.operation.inspect.read_deadline_is_retry_later', l9_run)
