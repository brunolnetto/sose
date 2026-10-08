# TRD-0003 — Durable Cross-Domain Composition for Trading Company PC6

- **Status:** Draft
- **Owner:** SOSE maintainers
- **Created:** 2026-10-08
- **Last updated:** 2026-10-08
- **Satisfies PRD(s):** PRD-0003
- **Related ADRs:** ADR-0006
- **Implementation PRs:** TBD

## 1. Technical context

SOSE process canonicals already own durable entities, commands/events, resources,
restart semantics, projections, and configuration independently. PC6 adds composition,
not a new shared business model.

The accepted process roadmap requires a minimum boundary contract with message
identity, source domain, destination contract, correlation/causation, idempotent
consumption, durable delivery/recovery, and explicit ownership.

## 2. Requirements mapping

| Requirement | Design response | Verification |
|---|---|---|
| FR-1 | immutable DomainBoundaryMessage envelope | schema/hash tests |
| FR-2 | payload-by-value + destination consumer | ownership tests |
| FR-3 | deterministic consumption identity | duplicate-delivery tests |
| FR-4 | durable pending/claimed/consumed delivery | restart tests |
| FR-5 | customer-demand composition fixture | E2E |
| FR-6 | replenishment composition fixture | E2E |
| FR-7 | accounting-output contracts | R2R integration tests |
| FR-8 | persisted correlation/causation | rebuild assertions |
| FR-9 | reconcile/retry loop | fault-injection tests |
| FR-10 | explicit ProcessManifest provenance | audit tests |

## 3. Proposed design

~~~mermaid
flowchart LR
    P[Producer domain] -->|immutable boundary contract| B[Durable boundary work]
    B --> C[Destination consumer]
    C -->|domain command/effect| D[Consumer-owned durable state]
    C --> A[Consumption acknowledgement]
    A --> B
~~~

The boundary layer owns delivery metadata only. Business semantics remain in typed
contract payloads and destination consumers.

### 3.1 Boundary envelope

Conceptual fields:

- message_id;
- contract_name;
- contract_version;
- source_domain;
- destination_domain;
- correlation_id;
- causation_id;
- produced_at logical time;
- canonical immutable payload.

message_id is deterministic from semantic source identity, contract, destination,
and occurrence key. Payload is hash-addressable.

### 3.2 Delivery state

Delivery has one durable identity per message/destination contract.

~~~text
PENDING -> CLAIMED -> CONSUMED
             |
             +---- lease expiry / restart ----> PENDING
~~~

A consumer acknowledgement becomes durable in the same SOSE operational transaction
as its consumer-owned business effect whenever both use the same authoritative
persistence boundary.

Future external transport that cannot share this transaction requires a separate
outbox/inbox ADR; this TRD does not imply distributed transactions.

### 3.3 Consumption

Consumers register handlers by destination contract, not by producer implementation type.

A handler:

1. validates contract/version;
2. derives deterministic consumer effect/command identity;
3. detects prior consumption;
4. changes only consumer-owned state;
5. records durable acknowledgement.

Duplicate delivery reuses the same semantic identities.

### 3.4 Initial W5 contracts

The initial named contracts are:

- o2c.fulfillment_requested.v1;
- warehouse.dispatch_ready.v1;
- logistics.delivery_completed.v1;
- o2c.payment_requested.v1;
- payments.settlement_completed.v1;
- accounting.entry_requested.v1;
- warehouse.replenishment_requested.v1;
- p2p.inventory_receipt_ready.v1.

The envelope is generic; payload schemas remain contract-specific.

## 4. Ownership matrix

| Concept | Durable owner |
|---|---|
| sales order / receivable | O2C |
| fulfillment order / allocations / pick-pack-ship | Warehouse Fulfillment |
| transport shipment / leg | Logistics |
| payment authorization / settlement | Cards & Payments |
| stock position / site / transfer | Warehouse Management |
| purchase order / supplier obligation | P2P |
| journal / reconciliation | R2R |
| delivery/consumption metadata | composition layer |

Foreign IDs may be stored as immutable values for correlation but do not transfer ownership.

## 5. Determinism and reproducibility

- boundary identities are deterministic;
- retry reuses semantic identity;
- correlation and causation are preserved exactly;
- handler ordering is deterministic under equal logical due time;
- wall-clock time does not participate in simulation semantics;
- continuous and rebuilt composition produce equal canonical durable outcomes.

## 6. Failure, recovery, and concurrency semantics

Authoritative composition state includes:

- immutable boundary envelope;
- delivery state;
- claim/lease/fencing metadata where applicable;
- consumer acknowledgement/effect identity;
- ordinary producer/consumer durable state.

Representative fault boundaries:

1. producer business commit before consume;
2. boundary claim before consumer effect;
3. consumer effect before acknowledgement;
4. acknowledgement before next reconciliation tick.

Concurrency rules:

- one active claim owns a delivery;
- stale fencing token cannot acknowledge;
- lease expiry returns unconsumed work to claimable state;
- duplicate contenders converge on one durable consumer effect;
- independent boundary messages may progress concurrently.

## 7. Observability

Read-only composition projection exposes:

- message/delivery identity;
- source/destination/contract/version;
- status;
- produced/claimed/consumed logical timestamps;
- correlation/causation;
- retry/claim count;
- consumer effect identity.

Composition KPIs are descriptive and never become operational truth.

## 8. Testing and conformance

Required:

- envelope canonicalization/hash tests;
- duplicate delivery tests;
- consumer idempotence tests;
- ownership/no-private-mutation tests;
- customer-demand E2E path;
- replenishment E2E path;
- accounting-output path;
- continuous-vs-rebuild equivalence;
- claim/worker death;
- stale fencing;
- persistence conformance where relevant;
- PC6 manifest provenance tests.

## 9. Compatibility and migration

Standalone domain APIs remain unchanged.

Composition consumers adapt typed contract payloads into existing domain commands or
domain-owned operations. A domain does not depend on the producer's Python entity class.

No existing PC5 maturity claim is weakened by this work.

## 10. Alternatives considered

### Direct cross-domain entity mutation
Rejected because ownership/replay/restart become ambiguous.

### Shared enterprise entity model
Rejected because the roadmap explicitly forbids a generic enterprise super-model.

### Generic event bus without durable consumption records
Rejected because retry/restart can duplicate business effects.

### External broker first
Rejected because transport technology should not define the semantic contract.

## 11. Implementation plan

- **PR A:** boundary envelope, durable delivery state, consumer registry, idempotence.
- **PR B:** customer-demand path O2C → WF → Logistics → O2C → Payments → R2R.
- **PR C:** replenishment/accounting path WM → P2P → WM plus R2R.
- **PR D:** restart/fencing/fault conformance across composed paths.
- **PR E:** projection/docs + ProcessManifest PC6 promotion.

## 12. Acceptance / exit criteria

- [ ] PRD-0003 acceptance criteria satisfied.
- [ ] Both W5 paths execute without direct private-state mutation.
- [ ] Duplicate delivery is business-idempotent.
- [ ] Continuous/rebuild composed state is equivalent.
- [ ] Correlation/causation survive restart.
- [ ] PC6 provenance is executable and complete.
- [ ] Standalone suites remain green.

## 13. Change history

| Date | Change | Rationale |
|---|---|---|
| 2026-10-08 | Initial draft | Technical plan for W5 Trading Company PC6 |
