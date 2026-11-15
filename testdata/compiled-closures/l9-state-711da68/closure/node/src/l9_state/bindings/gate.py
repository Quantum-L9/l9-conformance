from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from constellation_node_sdk import (
    NodeRuntimeConfig,
    TransportPacket,
    create_node_app,
    get_runtime_config,
    register_handler,
)
from pydantic import ValidationError

if TYPE_CHECKING:
    from fastapi import FastAPI

from ..execution import ExecutionBudget, ExecutionBudgetPolicy
from ..models import (
    StateAckRequest,
    StateClaimRequest,
    StateCreateRequest,
    StateEventReadRequest,
    StateHistoryRequest,
    StateListRequest,
    StateOperationInspectRequest,
    StateProblem,
    StateReadRequest,
    StateReleaseRequest,
    StateRenewRequest,
    StateRestoreRequest,
    StateTombstoneRequest,
    StateTransitionRequest,
)
from ..service import CallerContext, StateService

TENANT_BASE_AUTHORIZATION_POLICY = "l9.state.authorization/tenant-base@1"
STATE_MUTATION_ACTIONS = frozenset(
    {
        "state.create",
        "state.transition",
        "state.tombstone",
        "state.restore",
        "state.claim",
        "state.renew",
        "state.release",
        "state.ack",
    }
)


def _stable_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _delegation_evidence_refs(packet: TransportPacket) -> tuple[str, ...]:
    refs: list[str] = []
    for link in packet.delegation_chain:
        if link.proof is not None:
            refs.append(link.proof)
            continue
        refs.append(
            "gate.delegation:" + _stable_digest(link.model_dump(mode="json", exclude_none=True))
        )
    return tuple(refs)


def _tenant_base_authorization_ref(packet: TransportPacket) -> str:
    """Identify the explicit tenant-base decision from trusted Gate context.

    Packet identity is deliberately excluded.  The reference names the State policy
    plus the validated tenant/actor/delegation evidence that the policy evaluated.
    """

    evidence = {
        "policy": TENANT_BASE_AUTHORIZATION_POLICY,
        "tenant_org_id": packet.tenant.org_id,
        "principal_ref": packet.tenant.actor,
        "on_behalf_of": packet.tenant.on_behalf_of,
        "originator": packet.tenant.originator,
        "user_id": packet.tenant.user_id,
        "delegation_chain": [
            link.model_dump(mode="json", exclude_none=True) for link in packet.delegation_chain
        ],
    }
    return f"state.authz.tenant-base:{_stable_digest(evidence)}"


def _caller(
    packet: TransportPacket, *, budget_policy: ExecutionBudgetPolicy | None = None
) -> CallerContext:
    return CallerContext(
        tenant_org_id=packet.tenant.org_id,
        principal_ref=packet.tenant.actor,
        authorization_ref=_tenant_base_authorization_ref(packet),
        execution_budget=ExecutionBudget.start(packet.header.timeout_ms, policy=budget_policy),
        causation_ref=str(packet.header.packet_id),
        on_behalf_of_ref=packet.tenant.on_behalf_of,
        originator_ref=packet.tenant.originator,
        delegation_evidence_refs=_delegation_evidence_refs(packet),
    )


def _require_operation_identity(packet: TransportPacket, operation_id: str) -> None:
    if packet.header.idempotency_key != operation_id:
        raise ValueError("transport idempotency_key must equal request.operation_id")


def _invalid_request_problem(request: Any | None = None) -> StateProblem:
    fields: dict[str, object] = {
        "contract_version": "1.0",
        "status": "failed",
        "code": "INVALID_REQUEST",
        "retry_class": "no",
        "reason_codes": ("INVALID_REQUEST",),
    }
    if request is not None:
        scope_ref = getattr(request, "scope_ref", None)
        operation_id = getattr(request, "operation_id", None)
        if isinstance(scope_ref, str):
            fields["scope_ref"] = scope_ref
        if isinstance(operation_id, str):
            fields["operation_id"] = operation_id
    return StateProblem.model_validate(fields)


def _parse_request(
    model_type: Any,
    payload: dict[str, Any],
    packet: TransportPacket,
    *,
    action: str,
) -> Any | StateProblem:
    try:
        request = model_type.model_validate(payload)
    except ValidationError:
        return _invalid_request_problem()
    if action in STATE_MUTATION_ACTIONS:
        try:
            _require_operation_identity(packet, request.operation_id)
        except ValueError:
            return _invalid_request_problem(request)
    return request


def _dump(result: Any) -> dict[str, Any]:
    dumped: dict[str, Any] = result.wire_dump()
    return dumped


def bind_handlers(
    service: StateService, *, budget_policy: ExecutionBudgetPolicy | None = None
) -> None:
    @register_handler("state.create")
    async def state_create(
        _tenant: str, payload: dict[str, Any], packet: TransportPacket
    ) -> dict[str, Any]:
        request = _parse_request(StateCreateRequest, payload, packet, action="state.create")
        if isinstance(request, StateProblem):
            return _dump(request)
        return _dump(await service.create(_caller(packet, budget_policy=budget_policy), request))

    @register_handler("state.get")
    async def state_get(
        _tenant: str, payload: dict[str, Any], packet: TransportPacket
    ) -> dict[str, Any]:
        request = _parse_request(StateReadRequest, payload, packet, action="state.get")
        if isinstance(request, StateProblem):
            return _dump(request)
        return _dump(await service.get(_caller(packet, budget_policy=budget_policy), request))

    @register_handler("state.transition")
    async def state_transition(
        _tenant: str, payload: dict[str, Any], packet: TransportPacket
    ) -> dict[str, Any]:
        request = _parse_request(StateTransitionRequest, payload, packet, action="state.transition")
        if isinstance(request, StateProblem):
            return _dump(request)
        return _dump(
            await service.transition(_caller(packet, budget_policy=budget_policy), request)
        )

    @register_handler("state.tombstone")
    async def state_tombstone(
        _tenant: str, payload: dict[str, Any], packet: TransportPacket
    ) -> dict[str, Any]:
        request = _parse_request(StateTombstoneRequest, payload, packet, action="state.tombstone")
        if isinstance(request, StateProblem):
            return _dump(request)
        return _dump(await service.tombstone(_caller(packet, budget_policy=budget_policy), request))

    @register_handler("state.restore")
    async def state_restore(
        _tenant: str, payload: dict[str, Any], packet: TransportPacket
    ) -> dict[str, Any]:
        request = _parse_request(StateRestoreRequest, payload, packet, action="state.restore")
        if isinstance(request, StateProblem):
            return _dump(request)
        return _dump(await service.restore(_caller(packet, budget_policy=budget_policy), request))

    @register_handler("state.claim")
    async def state_claim(
        _tenant: str, payload: dict[str, Any], packet: TransportPacket
    ) -> dict[str, Any]:
        request = _parse_request(StateClaimRequest, payload, packet, action="state.claim")
        if isinstance(request, StateProblem):
            return _dump(request)
        return _dump(await service.claim(_caller(packet, budget_policy=budget_policy), request))

    @register_handler("state.renew")
    async def state_renew(
        _tenant: str, payload: dict[str, Any], packet: TransportPacket
    ) -> dict[str, Any]:
        request = _parse_request(StateRenewRequest, payload, packet, action="state.renew")
        if isinstance(request, StateProblem):
            return _dump(request)
        return _dump(await service.renew(_caller(packet, budget_policy=budget_policy), request))

    @register_handler("state.release")
    async def state_release(
        _tenant: str, payload: dict[str, Any], packet: TransportPacket
    ) -> dict[str, Any]:
        request = _parse_request(StateReleaseRequest, payload, packet, action="state.release")
        if isinstance(request, StateProblem):
            return _dump(request)
        return _dump(await service.release(_caller(packet, budget_policy=budget_policy), request))

    @register_handler("state.list")
    async def state_list(
        _tenant: str, payload: dict[str, Any], packet: TransportPacket
    ) -> dict[str, Any]:
        request = _parse_request(StateListRequest, payload, packet, action="state.list")
        if isinstance(request, StateProblem):
            return _dump(request)
        return _dump(
            await service.list_states(_caller(packet, budget_policy=budget_policy), request)
        )

    @register_handler("state.events")
    async def state_events(
        _tenant: str, payload: dict[str, Any], packet: TransportPacket
    ) -> dict[str, Any]:
        request = _parse_request(StateEventReadRequest, payload, packet, action="state.events")
        if isinstance(request, StateProblem):
            return _dump(request)
        return _dump(await service.events(_caller(packet, budget_policy=budget_policy), request))

    @register_handler("state.ack")
    async def state_ack(
        _tenant: str, payload: dict[str, Any], packet: TransportPacket
    ) -> dict[str, Any]:
        request = _parse_request(StateAckRequest, payload, packet, action="state.ack")
        if isinstance(request, StateProblem):
            return _dump(request)
        return _dump(
            await service.acknowledge(_caller(packet, budget_policy=budget_policy), request)
        )

    @register_handler("state.history")
    async def state_history(
        _tenant: str, payload: dict[str, Any], packet: TransportPacket
    ) -> dict[str, Any]:
        request = _parse_request(StateHistoryRequest, payload, packet, action="state.history")
        if isinstance(request, StateProblem):
            return _dump(request)
        return _dump(await service.history(_caller(packet, budget_policy=budget_policy), request))

    @register_handler("state.operation.inspect")
    async def state_operation_inspect(
        _tenant: str, payload: dict[str, Any], packet: TransportPacket
    ) -> dict[str, Any]:
        request = _parse_request(
            StateOperationInspectRequest, payload, packet, action="state.operation.inspect"
        )
        if isinstance(request, StateProblem):
            return _dump(request)
        return _dump(
            await service.inspect_operation(_caller(packet, budget_policy=budget_policy), request)
        )


def _validate_gate_runtime_config(config: NodeRuntimeConfig) -> None:
    required = {action.strip().lower() for action in config.require_idempotency_for_actions}
    missing = sorted(STATE_MUTATION_ACTIONS - required)
    if missing:
        raise ValueError(
            "Gate runtime must require idempotency for every State mutation; missing: "
            + ", ".join(missing)
        )


def create_app(
    service: StateService,
    *,
    budget_policy: ExecutionBudgetPolicy | None = None,
    config: NodeRuntimeConfig | None = None,
) -> FastAPI:
    resolved_config = config or get_runtime_config()
    _validate_gate_runtime_config(resolved_config)
    bind_handlers(service, budget_policy=budget_policy)
    return create_node_app(service_name="l9-state", version="0.1.0", config=resolved_config)
