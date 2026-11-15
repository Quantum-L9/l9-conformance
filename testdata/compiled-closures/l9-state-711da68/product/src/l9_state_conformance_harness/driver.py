"""State's conformance driver: the one composition of State under conformance.

``create_driver`` is the factory the compiled pytest/v2 suite names through
``L9_CONFORMANCE_DRIVER=l9_state_conformance_harness.driver:create_driver``; State's own fixture
runner (tests/fixture_case_runner.py) uses the same driver, so there is one composition, not two.
The call conventions follow the pytest/v2 driver contract as rendered by the compiler
(conftest.py sha256 97c8e7cf…06ae, test_l9_conformance.py lines 1-135 sha256 8d1d7153…768c;
decision record A10):

* the factory takes no arguments; one driver serves one case and is closed after it;
* all six methods are synchronous and may be called concurrently from several threads;
* results are plain JSON-like mappings (the Gate wire form); a StateProblem, conflict or
  refusal is returned as data, and only a harness or setup fault raises.

Composition: InProcessStateStore wrapped by FaultInjectingStore, FixtureClock as ``now``,
PolicyAuthorization as the ScopeAuthorizationPort when the AuthorizationPolicy control is
applied, and the environment of contracts/STATE_HARNESS_CONTRACTS.yaml. Interface actions go
through the bound Gate registration (``bind_handlers``), never through StateService directly.
Harness controls and observations dispatch by their contracts/STATE_HARNESS_SURFACE.yaml ref to
the realization each entry's ``implementation_ref`` names.

State's service is asynchronous: the driver owns one event loop on a background thread and runs
every call on it, so the store, its locks and the Gate handlers always share one loop.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import yaml
from constellation_node_sdk import create_transport_packet
from constellation_node_sdk.runtime.handlers import clear_handlers, get_handler
from pydantic import ValidationError

from l9_state.adapters.memory import InProcessStateStore
from l9_state.bindings.gate import bind_handlers
from l9_state.catalog import ConfiguredContractCatalog
from l9_state.cursor import CursorCodec
from l9_state.models import StateProblem
from l9_state.service import StateService

from .controls import (
    FaultInjectingStore,
    FixtureClock,
    PolicyAuthorization,
    hard_erase_current_revision,
    trim_journal,
)
from .observations import gate_registration, operation_receipt_count

ROOT = Path(__file__).parents[2]
TOPOLOGY_PATH = ROOT / "product-topology.yaml"
SURFACE_PATH = ROOT / "contracts/STATE_HARNESS_SURFACE.yaml"
CONTRACTS_PATH = ROOT / "contracts/STATE_HARNESS_CONTRACTS.yaml"

CLOCK = "urn:l9:state:harness:control:Clock:1.0"
JOURNAL = "urn:l9:state:harness:control:JournalRetention:1.0"
FAULT = "urn:l9:state:harness:control:StoreFault:1.0"
AUTHORIZATION = "urn:l9:state:harness:control:AuthorizationPolicy:1.0"
HARD_ERASE = "urn:l9:state:harness:control:HardErase:1.0"
RECEIPT_COUNT = "urn:l9:state:harness:observation:OperationReceiptCount:1.0"
GATE_REGISTRATION = "urn:l9:state:harness:observation:GateRegistration:1.0"
PROBLEM_CONTRACT = "urn:l9:state:StateProblem:1.0"

_T = TypeVar("_T")


class DriverSetupError(RuntimeError):
    """A harness or setup fault: the case is an error, never a pass or a fail."""


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        raise DriverSetupError(f"{path.name} is not a mapping")
    return loaded


def _validator(rule: str) -> Callable[[Mapping[str, Any]], bool]:
    if rule == "integer_value":
        return lambda payload: isinstance(payload.get("value"), int)
    if rule == "string_value":
        return lambda payload: isinstance(payload.get("value"), str)
    raise DriverSetupError(f"unknown schema acceptance rule {rule!r}")


@dataclass
class _Subject:
    """One isolated State: store, clock and the service bound to Gate."""

    clock: FixtureClock
    inner: InProcessStateStore
    store: FaultInjectingStore
    touched: bool = False


# A control adapter returns the control's result (None when its entry declares no result).
_ControlAdapter = Callable[["Driver", Mapping[str, Any]], Coroutine[Any, Any, Any]]
_ObservationAdapter = Callable[["Driver", Mapping[str, Any]], dict[str, Any]]


async def _clock(driver: Driver, request: Mapping[str, Any]) -> None:
    driver.subject.clock.advance(float(request["seconds"]))


async def _journal(driver: Driver, request: Mapping[str, Any]) -> None:
    await trim_journal(
        driver.subject.inner,
        tenant_org_id=driver.environment["tenant_ref"],
        scope_ref=driver.environment["scope_ref"],
        journal_seq=int(request["journal_seq"]),
    )


async def _fault(driver: Driver, request: Mapping[str, Any]) -> None:
    driver.subject.store.inject(request)


async def _authorization(driver: Driver, request: Mapping[str, Any]) -> None:
    # The policy is the ScopeAuthorizationPort the service is composed with, so it can only be
    # applied to an untouched subject: it must be a case's first given step.
    if driver.subject.touched:
        raise DriverSetupError("the authorization policy control must be the first given step")
    driver.compose(authorization=dict(request))


async def _hard_erase(driver: Driver, request: Mapping[str, Any]) -> dict[str, Any]:
    receipt = await hard_erase_current_revision(
        driver.subject.inner,
        tenant_org_id=driver.environment["tenant_ref"],
        scope_ref=driver.environment["scope_ref"],
        object_id=request["object_id"],
        operation_id=request["operation_id"],
    )
    return receipt.model_dump(mode="json", exclude_none=True)


def _receipt_count(driver: Driver, request: Mapping[str, Any]) -> dict[str, Any]:
    return operation_receipt_count(driver.subject.inner)


def _gate_registration(driver: Driver, request: Mapping[str, Any]) -> dict[str, Any]:
    return gate_registration(request["action"])


# State-owned call adapters, keyed by harness-surface entry ref.
CONTROL_ADAPTERS: dict[str, _ControlAdapter] = {
    CLOCK: _clock,
    JOURNAL: _journal,
    FAULT: _fault,
    AUTHORIZATION: _authorization,
    HARD_ERASE: _hard_erase,
}
OBSERVATION_ADAPTERS: dict[str, _ObservationAdapter] = {
    RECEIPT_COUNT: _receipt_count,
    GATE_REGISTRATION: _gate_registration,
}


class Driver:
    """The pytest/v2 conformance driver for one case (see the module docstring)."""

    def __init__(self) -> None:
        topology = _load(TOPOLOGY_PATH)
        surface = _load(SURFACE_PATH)
        self.environment: dict[str, Any] = dict(_load(CONTRACTS_PATH)["environment"])
        self._action_contracts: dict[str, str] = {
            action["id"]: action["result_contract_ref"]
            for action in topology["interfaces"]["actions"]
        }
        self._controls: dict[str, dict[str, Any]] = {
            entry["control_ref"]: entry for entry in surface["harness_controls"]
        }
        self._observations: dict[str, dict[str, Any]] = {
            entry["observation_ref"]: entry for entry in surface["harness_observations"]
        }
        # Fail closed: every declared entry has exactly one adapter and nothing else does.
        if set(self._controls) != set(CONTROL_ADAPTERS):
            raise DriverSetupError("harness controls and their call adapters differ")
        if set(self._observations) != set(OBSERVATION_ADAPTERS):
            raise DriverSetupError("harness observations and their call adapters differ")
        self._subject: _Subject | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="l9-state-conformance-driver", daemon=True
        )
        self._thread.start()

    # ----------------------------------------------------------------- composition

    @property
    def subject(self) -> _Subject:
        if self._subject is None:
            raise DriverSetupError("no subject: call fresh_subject() first")
        return self._subject

    def compose(self, *, authorization: Mapping[str, Any] | None = None) -> None:
        """Build a new isolated State and bind it to Gate."""
        environment = self.environment
        clock = FixtureClock()
        inner = InProcessStateStore(now=clock)
        store = FaultInjectingStore(inner)
        catalog = ConfiguredContractCatalog()
        for schema in environment["admitted_schemas"]:
            catalog.admit_schema(schema["ref"], schema["digest"], _validator(schema["accepts"]))
        for retention in environment["admitted_retention_policies"]:
            catalog.admit_retention(retention["ref"], retention["digest"])
        service = StateService(
            store,
            catalog,
            cursor_codec=CursorCodec(environment["cursor_secret"].encode("utf-8")),
            authorization=None if authorization is None else PolicyAuthorization(authorization),
        )
        clear_handlers()
        bind_handlers(service)
        self._subject = _Subject(clock=clock, inner=inner, store=store)

    def _run(self, coroutine: Coroutine[Any, Any, _T]) -> _T:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()

    # ------------------------------------------------------------- pytest/v2 driver

    def fresh_subject(self) -> None:
        self.compose()

    def interface_action(
        self, operation: str, request: Mapping[str, Any], context: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        if operation not in self._action_contracts:
            raise DriverSetupError(f"{operation} is not a declared State interface action")
        if context is None:
            raise DriverSetupError(f"{operation}: a State Gate call needs an invocation context")
        subject = self.subject
        subject.touched = True
        return self._run(_gate_call(operation, dict(request), dict(context)))

    def result_contract(self, operation: str, result: Any) -> str | None:
        if operation in self._action_contracts:
            try:
                StateProblem.model_validate(result)
            except ValidationError:
                return self._action_contracts[operation]
            return PROBLEM_CONTRACT
        if operation in self._controls:
            declared: str | None = self._controls[operation].get("result_contract_ref")
            return declared
        if operation in self._observations:
            observed: str = self._observations[operation]["result_contract_ref"]
            return observed
        raise DriverSetupError(f"{operation} is neither a State action nor a harness entry")

    def harness_control(self, control_ref: str, request: Mapping[str, Any] | None) -> Any:
        adapter = CONTROL_ADAPTERS.get(control_ref)
        if adapter is None:
            raise DriverSetupError(f"undeclared harness control {control_ref}")
        subject = self.subject
        result = self._run(adapter(self, request or {}))
        if control_ref != AUTHORIZATION:
            subject.touched = True
        return result

    def harness_observation(
        self, observation_ref: str, request: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        adapter = OBSERVATION_ADAPTERS.get(observation_ref)
        if adapter is None:
            raise DriverSetupError(f"undeclared harness observation {observation_ref}")
        return adapter(self, request or {})

    def port_fixture(self, operation: str, request: Mapping[str, Any] | None) -> Any:
        raise DriverSetupError(f"State declares no setup ports ({operation})")

    def close(self) -> None:
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()
        clear_handlers()


async def _gate_call(
    operation: str, request: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    principal = context["principal_ref"]
    tenant = {
        "org_id": context["tenant_ref"],
        "actor": principal,
        "on_behalf_of": principal,
        "originator": principal,
    }
    packet = create_transport_packet(
        action=operation,
        payload=request,
        tenant=tenant,
        timeout_ms=int(context["budget_ms"]),
        idempotency_key=request.get("operation_id"),
    )
    handler = get_handler(operation)
    if handler is None:
        raise DriverSetupError(f"Gate has no handler for {operation}")
    result = await handler(tenant["org_id"], request, packet)
    if not isinstance(result, dict):
        raise DriverSetupError(f"{operation} returned {type(result).__name__}, not a mapping")
    return result


def create_driver() -> Driver:
    """The pytest/v2 driver factory (``L9_CONFORMANCE_DRIVER`` names it)."""
    return Driver()
