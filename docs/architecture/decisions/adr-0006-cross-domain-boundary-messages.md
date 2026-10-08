# ADR-0006 — Cross-domain composition uses immutable durable boundary messages

- **Status:** Accepted
- **Date:** 2026-10-08
- **Decision owners:** SOSE maintainers
- **Related PRD(s):** PRD-0003
- **Related TRD(s):** TRD-0003
- **Supersedes:** none
- **Superseded by:** none

## Context

PC6 requires stable ingress/egress contracts and tested cross-domain execution.
Participating domains already own independent durable business state. Passing mutable
entities between domains or letting one domain write another domain's state would
destroy ownership, restart equivalence, and replay safety.

The composition boundary must be durable enough for retry/restart while remaining
small enough not to become a generic enterprise ontology or prescribe an external
message broker.

## Decision

SOSE cross-domain composition uses an immutable, deterministic, durable boundary-message
envelope plus durable delivery/consumption state.

The envelope owns composition metadata only:

- deterministic message identity;
- contract name and version;
- source and destination domain;
- correlation and causation;
- logical production time;
- canonical immutable payload.

Producer domains own emitted business facts. Destination consumers translate payloads
into consumer-owned commands/effects. Consumers never mutate producer private state.

Duplicate delivery is resolved through deterministic consumption identity and durable
acknowledgement/effect evidence.

For domain concepts that historically have standalone local ownership but move to a
different owner in composition, the composition adapter **replaces** the local owner.
It must not mirror a second authoritative copy.

The first explicit case is inventory:

- Warehouse Fulfillment standalone keeps its current local InventoryLot semantics.
- Trading Company composed mode assigns stock position/reservation ownership to
  Warehouse Management.
- Composed Warehouse Fulfillment receives immutable WM reservation/availability IDs
  and requests stock effects through boundary contracts; it does not mutate or mirror
  authoritative stock quantities locally.

## Decision drivers

- explicit business ownership;
- restart/replay safety;
- deterministic identity;
- idempotent retry;
- no generic enterprise super-model;
- compatibility with existing SOSE persistence/reconciliation semantics.

## Consequences

### Positive

- domain boundaries become auditable and replay-safe;
- composed paths survive process restart;
- consumers depend on contracts instead of implementation classes;
- cross-domain causality remains explicit;
- future external transports have a precise semantic contract to preserve.

### Negative / trade-offs

- payload/version evolution needs compatibility discipline;
- composition introduces durable delivery metadata;
- consumer adapters are explicit work;
- exactly-once transport is not assumed; business effects rely on idempotence.

### Constraints introduced

- no mutable entity object crosses a domain ownership boundary;
- composed-mode ownership adapters replace conflicting standalone ownership rather
  than synchronizing two authoritative truths;
- every message and consumption has deterministic semantic identity;
- correlation/causation survives persistence;
- retry reuses identity;
- PC6 requires tested composed execution, not declarations alone;
- an external broker/outbox-inbox architecture requires a future ADR.

## Alternatives considered

### Direct entity references

Rejected because ownership and restart behavior become ambiguous.

### Shared database tables as the integration contract

Rejected because persistence layout is not a business-domain contract.

### Generic enterprise event ontology

Rejected because the process roadmap explicitly avoids a generic enterprise super-model.

### External Kafka/broker as the first implementation

Rejected because transport technology should not define composition semantics.

## Compatibility and migration

Standalone domain behavior remains unchanged. Composition is additive.

Existing deterministic identity, command/event correlation, leases/fencing, and
persistence transactions should be reused where their semantics fit.

Any future external transport must preserve this ADR's semantics or supersede it.

## Verification

Conformance proves:

- canonical deterministic message identity;
- immutable payload semantics;
- one durable owner per business concept;
- idempotent duplicate delivery;
- restart-equivalent composed execution;
- preserved correlation/causation;
- no direct cross-domain private-state mutation;
- Warehouse Fulfillment composed mode contains no authoritative duplicate of
  Warehouse Management stock/reservation truth while standalone mode remains valid.

## References

- docs/architecture/process-canonical-roadmap.md
- docs/architecture/process-dependency-graph.md
- docs/product/requirements/prd-0003-trading-company-pc6.md
- docs/technical/requirements/trd-0003-trading-company-pc6.md
