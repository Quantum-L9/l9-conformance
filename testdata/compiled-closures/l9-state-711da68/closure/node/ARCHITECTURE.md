<!-- l9:compiled-document schema=l9.compiled-documents/v1 document=ARCHITECTURE.md generator=l9-semantic-compiler binding=l9.binding/node-birth-package@1 authority_status=candidate_not_admitted -->
<!-- Compiled from the node's semantic inputs. Do not edit: a hand edit outside the product-prose region is drift. Regenerate with l9-semantic-compiler. -->

# Architecture: l9-state

## Identity

- Repository: `Quantum-L9/l9-state`
- Product: `l9.state`
- Semantic owner: `l9-state`
- Product kind: `node`
- Archetype: `l9.node-archetype/authoritative-state@1`

Identity dimensions (ProductManifest `identity.dimensions`):

| Dimension | Identity |
| --- | --- |
| `constellation` | `unresolved` |
| `product` | `l9.state` |
| `release` | `unresolved` |
| `runtime` | `unresolved` |

## Lifecycle

- Repository status: `candidate` (RepositorySpec `repository.status`)
- Product status: `candidate` (ProductTopology `product.status`)
- Generated artifacts: `candidate_not_admitted` until a `.github` binding admits `l9.binding/node-birth-package@1`
- Realized: false (requires a final conformance receipt from l9-conformance)

Applicable lifecycle states (ProductManifest `lifecycle.applicable_state_refs`):
- `candidate`
- `admission_evaluated`
- `awaiting_authority`
- `admitted`
- `published`
- `current`
- `superseded`
- `retired`

## Ports

Inbound:

| Port | Name | Implementation |
| --- | --- | --- |
| `urn:l9:state:port:GateActions:1.0` | `state.* Gate action surface` | `src/l9_state/service.py::StateService` |

Outbound:

| Port | Name | Implementation |
| --- | --- | --- |
| `urn:l9:state:port:StateStore:1.0` | `StateStorePort` | `src/l9_state/ports.py::StateStorePort` |
| `urn:l9:state:port:ContractCatalog:1.0` | `ContractCatalogPort` | `src/l9_state/ports.py::ContractCatalogPort` |
| `urn:l9:state:port:ScopeAuthorization:1.0` | `ScopeAuthorizationPort` | `src/l9_state/ports.py::ScopeAuthorizationPort` |

## Adapters

| Group | Adapter | Realizes port | Implementation |
| --- | --- | --- | --- |
| `inbound` | `urn:l9:state:adapter:GateSDK:1.0` | `urn:l9:state:port:GateActions:1.0` | `src/l9_state/bindings/gate.py::bind_handlers` |
| `provider` | `urn:l9:state:adapter:MongoStateStore:1.0` | `urn:l9:state:port:StateStore:1.0` | `src/l9_state/adapters/mongodb.py` |
| `provider` | `urn:l9:state:adapter:MemoryStateStore:1.0` | `urn:l9:state:port:StateStore:1.0` | `src/l9_state/adapters/memory.py` |
| `integration` | `urn:l9:state:adapter:ConfiguredContractCatalog:1.0` | `urn:l9:state:port:ContractCatalog:1.0` | `src/l9_state/catalog.py` |

Forbidden adapters (ProductManifest `adapters.forbidden`):
- `gate_registered_hard_erase`

## Gate and transport law

ProductKind `node` (pinned `.github` ProductKind catalog) requires:
- `independent_runtime_identity`
- `independent_health_and_readiness`
- `canonical_constellation_communication`
- `gate_routed_inter_product_work`
- `runtime_admission`
- `actor_identity_when_consequential_authorship_exists`
- `surface_identity_when_multiple_execution_surfaces_exist`

and forbids:
- `peer_import_of_server_implementation_as_canonical_consumption`
- `direct_peer_routing_when_gate_is_required`
- `provider_identity_as_product_identity`

Required architecture patterns:
- `l9.pattern/runnable-node@1`
- `l9.pattern/semantic-core-gate-adapter@1`

Product hard constraints (ProductManifest `requirements.hard_constraints`):
- all inter-product traffic MUST be Gate-mediated; no peer routing and no peer URLs
- every State mutation MUST commit projection, immutable revision, journal event, and operation receipt atomically
- historical revisions MUST NOT be mutated; corrections create new revisions
- one scoped operation_id MUST bind exactly one action and canonical request digest
- every mutation action MUST require transport idempotency_key equal to request operation_id
- stale fences MUST NOT authorize mutation
- public contracts MUST NOT contain storage-provider syntax or identifiers
- Gate timeout MUST remain the sole request-budget authority; State may shrink but never extend it
- hard erase MUST NOT be registered with Gate or exposed by StateClient
- a RetentionExecutor MUST remain unbound until an exact externally owned retention-decision contract is bound

Runtime identity bindings (ProductManifest `identity.bindings`):
- `caller_actor` from Gate TransportPacket tenant.actor / tenant.on_behalf_of / tenant.originator / delegation_chain: State records caller principal and delegation evidence on receipts; it does not author ActorIdentity or SurfaceIdentity values

Target binding `l9.binding/python-gate-node@1` rules:
- `consumes_only_implementation_ir_lowered_from_a_manifest_satisfying_the_resolved_manifest_gate`
- `every_registered_action_is_exactly_one_manifest_interfaces_action_and_every_manifest_action_is_registered`
- `registered_actions_are_reached_only_through_gate_sdk_transport`
- `every_manifest_effects_consequential_operation_requires_gate_runtime_idempotency`
- `forbidden_actions_are_never_registered`
- `port_and_adapter_implementations_are_bound_by_manifest_implementation_ref_and_verified_not_regenerated`
- `behavior_code_is_emitted_only_where_an_admitted_behavior_contract_determines_it`
- `unresolved_action_contract_effect_port_adapter_or_implementation_ref_fails_closed`
- `optional_port_without_a_declared_adapter_is_not_given_an_invented_adapter`
- `binding_emits_no_http_route_authentication_peer_url_or_routing_logic`
- `generated_artifacts_are_derived_and_never_semantic_authority`
- `final_conformance_is_required_before_generated_artifacts_count_as_realized`
- `existing_target_bytes_have_no_authority_or_preservation_privilege`
- `rendering_is_deterministic_and_semantically_noncreative`

## Conformance

Required conformance classes (ProductManifest `conformance.required_classes`):
- `core`
- `capability`
- `runtime`
- `adapter`

- Final conformance required before realization: true

## Boundaries

Owns (RepositorySpec `ownership.owns`):
- `state_implementation_source`
- `state_contract_schemas`
- `state_reason_code_catalog`
- `state_invariant_catalog`
- `state_validation_tests`
- `state_semantic_fixtures`
- `state_operation_semantics`
- `state_conformance_fixture_cases`

Does not own (RepositorySpec `ownership.does_not_own`):
- `global_l9_semantics`
- `ProductTopology_semantics`
- `ProductKind_semantics`
- `global_identity_semantics`
- `Gate_transport_or_routing`
- `storage_provider_semantics`
- `product_admission_authority`
- `repository_birth_machinery`
- `repository_merge_authority`
- `deployment_authority`

Product boundary does not own (ProductTopology `boundary.does_not_own`):
- Work Item meaning
- consumer business lifecycle
- next-action selection
- source-system business truth
- memory/context semantics
- global scheduling
- Program Execution authority
- transport routing
- retention-policy authority
- repository birth machinery
- storage provider semantics (MongoDB is a private provider)
- payload schema meaning (schema meaning remains external; State binds schema_ref and digest)

Product non-goals (ProductTopology `purpose.non_goals`):
- Work Item meaning
- consumer business lifecycle
- next-action selection
- source-system business truth
- memory or context semantics
- global scheduling
- Program Execution authority
- transport routing
- retention-policy authority
- repository birth machinery

## Repository contracts

- `.l9/architecture.yaml` (`l9.architecture-spec/v1`)
- `.l9/ci.json` (`l9.ci-consumer/v1`)
- `.l9/gate-registration.json` (`l9.gate-registration-manifest/v1`)
- `.l9/ownership.yaml` (`l9.ownership-spec/v1`)
- `.l9/repo-contracts.json` (`l9.repo-contracts/v1`)

## Provenance

This file is derived from the inputs below and restates no product meaning of its own. Where it and they disagree, they win.

- product_topology: `product-topology.yaml`
- repository_spec: `repository-spec.yaml`
- workflow_spec: `workflow-spec.yaml`
- authority_lock: `semantics.lock.yaml`
- Upstream semantic authority: `Quantum-L9/.github`
