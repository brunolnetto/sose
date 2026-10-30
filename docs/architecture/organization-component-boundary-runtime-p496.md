# P496-A — owner-qualified organizational boundary runtime

**Status:** implementation slice in Draft PR; it is not the complete #496 runtime.

## Scope

This increment adds an explicit organization/component binding to a durable
boundary publication and routes the delivery through the exact admitted
component pair. It composes existing SOSE primitives; it does not introduce a
super-StateChart, organization-wide clock, second business-state owner or
distributed transaction.

A v2 scoped **BoundaryMessage** carries:

- organization ID, admitted definition fingerprint and configuration epoch;
- producer component ID and immutable process owner ID;
- consumer component ID and immutable process owner ID.

All seven values are mandatory together. Partial bindings fail closed. The
v2 deterministic message identity includes the organization, definition
fingerprint, epoch, both component identities and both owner identities, so
the same semantic occurrence in a second organization or under another owner
cannot collide with the first.

Messages created without these fields retain the existing v1 message-ID
derivation. The persistence codec also omits the new optional None fields
for v1 messages, preserving their tagged serialization shape. The v1
domain-level **BoundaryConsumerRegistry** remains available and is not silently
upgraded into organization ownership evidence.

## Admission and execution path

**OrganizationBoundaryConsumerRegistry** is built from:

1. an immutable **OrganizationDefinition**;
2. the actual **DomainCatalog** and audited process manifests;
3. the existing domain-level **BoundaryConsumerRegistry**, used only as the
   audited contract catalogue.

The constructor reruns **validate_organization_admission**. A runtime handler
must then be registered for an exact (producer_component_id,
consumer_component_id, contract_name, contract_version) edge declared by
that definition. Dispatch verifies the message fingerprint and epoch,
component IDs, domains, owners and contract edge before returning the handler.

**OrganizationBoundaryService.publish** requires typed causation:

- a **DomainEvent** whose owner, entity type, correlation and logical time agree
  with the declared producer component; or
- a previously persisted, organization-scoped boundary message whose
  destination component/owner is exactly the next message's source
  component/owner and whose organization and correlation match.

The existing **BoundaryService** still owns durable publication, deterministic
delivery identity, causal ordering, lease epochs, ACK and atomic handler/ACK
transaction. The organization facade claims by exact **message_id**; it does
not use the generic domain-wide scan to pick a message that may belong to a
different component or organization.

## Explicit non-claims / remaining P496 work

This slice proves component-bound route identity and owner-qualified source
causation, not every acceptance criterion in #496.

- It does not yet instantiate **SimulationJob** objects from **ScheduledJob**,
  persist organization/component attestation on job checkpoints, or run
  independent component triggers.
- It does not yet provide the canonical multi-source organization snapshot
  across StateCharts, Commands, queues, boundary protocol, physical
  reservations and job positions.
- It does not admit operational policy/calendar/cost references or physical
  pools; static admission intentionally remains fail-closed for those.
- A handler is still trusted domain code operating through the existing UoW.
  This route envelope is not a protected writer-role/key boundary and does
  not replace #545 authority hardening.
- It does not prove PostgreSQL multi-worker/restart equivalence, finite shared
  pool arbitration or #477 physical-causal admission. Those remain separate
  gates, not inferred from these unit tests.

The next P496 increment must bind each declared job to its exact organization,
component and owner through durable execution evidence, then compose a
consistent derived read model without a new writable organization aggregate.
P578/#477 remain the physical execution gates; P497 remains the O2C/MRO
heterogeneous falsifier. Frozen Manufacturing/O2C/MRO v1 evidence is untouched.
