<!-- l9:compiled-document schema=l9.compiled-documents/v1 document=README.md generator=l9-semantic-compiler binding=l9.binding/node-birth-package@1 authority_status=candidate_not_admitted -->
<!-- Compiled from the node's semantic inputs. Do not edit: a hand edit outside the product-prose region is drift. Regenerate with l9-semantic-compiler. -->

# l9-state

Serve as the canonical source repository for the l9.state Node implementation, its public State contracts, and its validation fixtures.

## Status

- Repository status: `candidate` (RepositorySpec `repository.status`)
- Product status: `candidate` (ProductTopology `product.status`)
- Generated artifacts: `candidate_not_admitted` until a `.github` binding admits `l9.binding/node-birth-package@1`
- Realized: false (requires a final conformance receipt from l9-conformance)

## Identity

- Repository: `Quantum-L9/l9-state`
- Product: `l9.state`
- Semantic owner: `l9-state`
- Product kind: `node`
- Archetype: `l9.node-archetype/authoritative-state@1`

## Purpose

Provide durable, provider-neutral, revisioned State authority to Constellation participants exclusively through Gate, preserving immutable revisions, exact operation identity, conditional mutation, canonical operation receipts, fenced claims, a canonical pull journal with consumer checkpoints, bounded history, and tombstone/retained-restore lifecycle mechanics.

Primary responsibility: Own authoritative StateRecord identity and its revisioned mutation mechanics behind a Gate-routed node boundary, without owning the business meaning, lifecycle, scheduling, or next-action selection of any consumer domain.

## Capabilities

- `l9.capability/state`

## Public actions

Gate and transport law: [ARCHITECTURE.md](ARCHITECTURE.md).

| Action | Request contract | Result contract | Effect class |
| --- | --- | --- | --- |
| `state.create` | `urn:l9:state:StateCreateRequest:1.0` | `urn:l9:state:StateReceipt:1.0` | `state_mutation` |
| `state.get` | `urn:l9:state:StateReadRequest:1.0` | `urn:l9:state:StateReadResult:1.0` | `read_observation` |
| `state.transition` | `urn:l9:state:StateTransitionRequest:1.0` | `urn:l9:state:StateReceipt:1.0` | `state_mutation` |
| `state.tombstone` | `urn:l9:state:StateTombstoneRequest:1.0` | `urn:l9:state:StateReceipt:1.0` | `state_mutation` |
| `state.restore` | `urn:l9:state:StateRestoreRequest:1.0` | `urn:l9:state:StateReceipt:1.0` | `state_mutation` |
| `state.claim` | `urn:l9:state:StateClaimRequest:1.0` | `urn:l9:state:StateClaimReceipt:1.0` | `state_mutation` |
| `state.renew` | `urn:l9:state:StateRenewRequest:1.0` | `urn:l9:state:StateClaimReceipt:1.0` | `state_mutation` |
| `state.release` | `urn:l9:state:StateReleaseRequest:1.0` | `urn:l9:state:StateClaimReceipt:1.0` | `state_mutation` |
| `state.list` | `urn:l9:state:StateListRequest:1.0` | `urn:l9:state:StateListPage:1.0` | `read_observation` |
| `state.events` | `urn:l9:state:StateEventReadRequest:1.0` | `urn:l9:state:StateEventPage:1.0` | `read_observation` |
| `state.ack` | `urn:l9:state:StateAckRequest:1.0` | `urn:l9:state:StateAckReceipt:1.0` | `state_mutation` |
| `state.history` | `urn:l9:state:StateHistoryRequest:1.0` | `urn:l9:state:StateHistoryPage:1.0` | `read_observation` |
| `state.operation.inspect` | `urn:l9:state:StateOperationInspectRequest:1.0` | `urn:l9:state:StateOperationInspectResult:1.0` | `read_observation` |

Consequential actions:
- `state.create`
- `state.transition`
- `state.tombstone`
- `state.restore`
- `state.claim`
- `state.renew`
- `state.release`
- `state.ack`

Forbidden actions (never registered):
- `state.hard_erase`
- `state.hard_delete`

## Running

- Package: `l9_state_node`
- Distribution: l9-state-node 0.1.0
- Entrypoint: `unresolved`: the RepositorySpec declares no node_runtime construction graph

- Facade: `unresolved`: the RepositorySpec declares no commands.repository_execution (Core-generated `Makefile`; never hand-edited)
- Implementation: `unresolved`: the RepositorySpec declares no commands.repository_execution
- Phases: `unresolved`: the RepositorySpec declares no commands.repository_execution.phases
- Repository-semantic validate command: python -m pytest

## What this repository does not own

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
