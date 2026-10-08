# TRD-0002 — Warehouse Fulfillment Operational Capacity, Durability, and Observability

- **Status:** Accepted
- **Owner:** SOSE maintainers
- **Created:** 2026-10-08
- **Last updated:** 2026-10-08
- **Satisfies PRD(s):** PRD-0002
- **Related ADRs:** ADR-0001, ADR-0002
- **Implementation PRs:** TBD

## 1. Technical context

The current implementation performs allocation, pick, pack, and ship synchronously. Inventory ownership and immutable movement evidence are strong, but there is no contested operational service capacity and no processing-time semantics.

The existing audit therefore correctly stops at PC2 even though some higher-gate durability primitives already exist.

## 2. Requirements mapping

| Requirement | Design response | Verification |
|---|---|---|
| FR-1..3 | durable resource definitions for picker, packing station, shipping dock | resource tests |
| FR-4 | ResourceDemand/reservation gating | contention tests |
| FR-5 | positive service durations plus durable completion work | time-semantics tests |
| FR-6 | deterministic operation identity and replay-safe completion | restart tests |
| FR-7 | continuous-vs-rebuild operational snapshot | e2e equivalence |
| FR-8 | release/recovery cleanup | crash/fault tests |
| FR-9 | read-only projection | unit tests |
| FR-10 | canonical fulfillment KPIs | unit tests |
| FR-11 | validated config fields plus documentation | config/spec tests |
| FR-12 | unchanged allocation/correction invariants | existing regression suite |

## 3. Proposed design

### 3.1 Service resources

The domain owns three service-resource names:

- fulfillment_picker
- packing_station
- shipping_dock

Capacities are positive integers from configuration.

Inventory lots remain entities/material state and are not represented as Resources.

### 3.2 Operation lifecycle

Each service stage uses a deterministic operation/request identity derived from order/allocation plus stage.

A domain-owned FulfillmentServiceTask records operational progress without polluting the business StateCharts with queueing states. Its lifecycle is queued -> in_progress -> completed.

Each task also persists the timing evidence needed after transient resource records are cleaned up:

- requested_at: durable stage-eligibility/request timestamp;
- acquired_at: copied from the durable ResourceReservation when capacity is granted;
- service_duration: configured duration frozen for that task;
- completion_due_at: acquired_at plus service_duration.

The immutable transition event into completed is the authoritative completion timestamp. Therefore queue delay is acquired_at - requested_at and service delay is completion_event.occurred_at - acquired_at even after ResourceDemand, ResourceReservation, and consumed ScheduledWork have been removed.

~~~mermaid
flowchart LR
  E[Business stage eligible] --> T[FulfillmentServiceTask queued]
  T --> Q[Durable ResourceDemand]
  Q --> R[Reservation acquired]
  R --> I[ServiceTask in_progress]
  I --> S[Durable completion ScheduledWork]
  S --> C[ServiceTask completed]
  C --> O[Reconcile business transition + immutable occurrence]
  O --> X[Resource release]
~~~

The service duration starts only after resource acquisition. A queued request has no completion schedule yet.

Reconciliation is responsible for closing the crash window between a committed resource grant and creation of completion ScheduledWork. On every reconcile pass, if a ServiceTask has a durable reservation but no pending completion work, the domain copies the reservation acquired_at into the task if needed, computes the deterministic completion_due_at, transitions/keeps the task in_progress, and idempotently ensures the completion command/schedule exists. Rebuild of the backend reservation alone is therefore never relied upon to replay a lost scheduling callback.

The business entity does not claim completion merely because service work was queued or started. After ServiceTask completion, domain reconciliation applies the existing business transition and its side effects idempotently, then releases resource capacity. If a crash occurs between operational completion and business reconciliation, restart observes the completed task and safely finishes the business effect.

This keeps queue/in-service truth in domain operational records plus SOSE resource/schedule primitives, while FulfillmentOrder and Allocation StateCharts continue to represent business lifecycle truth.

### 3.3 Picking boundary

For each allocation:

1. order enters or has entered picking;
2. deterministic picker demand is requested;
3. when acquired, a deterministic pick-completion command is scheduled;
4. on completion, the existing deterministic pick occurrence applies the physical inventory decrement exactly once;
5. Allocation transitions committed to picked;
6. picker capacity is released.

The inventory decrement must not happen when merely queued or reserved.

### 3.4 Packing boundary

Packing becomes eligible only when all allocations are picked.

The order waits for packing_station, schedules completion, writes the existing pack occurrence, transitions to packed, then releases capacity.

### 3.5 Shipping boundary

Shipping becomes eligible only from packed.

The order waits for shipping_dock, schedules completion, transitions allocations to shipped, writes the ship occurrence, transitions order to shipped, then releases capacity.

## 4. Interfaces and contracts

Configuration adds:

- picker_capacity: integer >= 1
- packing_station_capacity: integer >= 1
- shipping_dock_capacity: integer >= 1
- pick_duration: timedelta > 0
- pack_duration: timedelta > 0
- ship_duration: timedelta > 0

Existing fields and standalone entry points remain compatible where practical.

Operational helpers may be split into request/reconcile-completion functions instead of preserving synchronous semantics internally.

## 5. Data model and ownership

Existing durable owners remain unchanged:

- FulfillmentOrder: lifecycle;
- Allocation: reservation/fulfillment ownership;
- InventoryLot: material projection;
- InventoryOccurrence: immutable material/packing/shipping evidence.

New durable operational truth uses existing SOSE primitives:

- ResourceDefinition
- ResourceDemand
- ResourceReservation
- ResourceReleaseIntent
- Command
- ScheduledWork

One domain-specific FulfillmentServiceTask entity is introduced to separate operational service progress from business lifecycle state and to retain stage timing evidence after transient resource/schedule records are cleaned up. No new generic kernel entity or warehouse abstraction is introduced.

## 6. Determinism and reproducibility

Deterministic keys include stage plus order/allocation identity plus occurrence ordinal.

Retries/rebuilds discover existing demand/schedule/occurrence identities instead of creating replacements.

No correctness rule depends on mutable RNG consumption order.

## 7. Failure, recovery, and concurrency semantics

Required recovery boundaries:

1. picker demand queued before acquisition;
2. reservation acquired but ServiceTask timing/schedule not yet persisted;
3. ServiceTask in_progress with missing completion ScheduledWork;
4. picker acquired with pick completion scheduled;
5. ServiceTask completed but business pick effect not yet reconciled;
6. restart after pick occurrence committed but before cleanup;
7. packing demand queued/acquired;
8. shipping ServiceTask completion pending;
9. post-completion release-intent recovery.

For boundaries 2 and 3, reconciliation must reconstruct the missing task metadata/schedule from durable reservation state and deterministic task identity. A reservation is never allowed to hold capacity indefinitely merely because the process crashed before schedule creation.

Continuous and rebuilt runs compare complete operational snapshots, not only final entity states.

A crash after business transition but before resource cleanup must be repairable idempotently.

## 8. Performance and scalability

Reference capacities are intentionally small to make contention executable in tests.

Per-order reconciliation should remain proportional to its allocations and active operational records.

## 9. Security and privacy

Not applicable beyond ordinary repository/runtime controls: this reference uses synthetic data only.

## 10. Observability

Projection should expose at minimum:

- order/state;
- allocation states/counts;
- material quantities;
- completion status;
- lead time;
- active/queued service stage when derivable;
- substitution indicator;
- durable ServiceTask timing/status for each stage.

KPIs should include at minimum:

- fulfillment lead time;
- transition count;
- allocation/substitution counts;
- picked quantity;
- per-stage queue delay derived from ServiceTask requested_at/acquired_at;
- per-stage service time derived from acquired_at and immutable completion event;
- completion status.

Completed-order timing metrics must not depend on ResourceDemand, ResourceReservation, or consumed ScheduledWork still being present.

Exact fields are frozen in the PC5 implementation PR before manifest promotion.

## 11. Testing and conformance

Required:

- existing statechart/happy/sad path suites;
- service-capacity tests;
- contention with simultaneous eligible demand;
- explicit time progression tests;
- continuous-vs-rebuild restart equivalence;
- explicit reservation-acquired/no-schedule recovery regression;
- completed-stage timing survives demand/reservation/schedule cleanup;
- cleanup/fault-recovery tests;
- read-only projection/KPI tests;
- process-manifest provenance tests.

## 12. Migration and compatibility

Existing synchronous helper functions may remain as compatibility wrappers only if they preserve the new operational semantics. They must not bypass finite resources or configured duration.

Existing deterministic inventory occurrence IDs remain unchanged.

Config additions have safe positive defaults.

## 13. Alternatives considered

### Treat inventory quantity as capacity

Rejected: stock is material/business state, not picker/packing/dock service capacity.

### Use only backend-local SimPy resources/delays

Rejected: queued/in-progress intent would not be authoritative across restart.

### Keep synchronous operations and attach fake timestamps

Rejected: this would satisfy documentation but not PC3 operational semantics.

### Introduce a generic warehouse-service abstraction

Rejected until another materially different domain demonstrates the same contract.

## 14. Implementation plan

1. PR A — finite service resources plus explicit stage durations and contention.
2. PR B — restart equivalence plus fault/cleanup recovery across operational boundaries.
3. PR C — projection/KPIs/specification plus PC5 manifest promotion.

PRs may be combined only if reviewability and failure isolation remain clear.

## 15. Risks and open questions

| Item | Impact | Mitigation |
|---|---|---|
| Reservation held across restart incorrectly | high | compare durable resource state in restart snapshots |
| Completion command duplicated | high | deterministic command/schedule identity |
| Inventory decremented before service completion | high | invariant test at queued/reserved boundary |
| Compatibility helper bypasses delay | high | tests assert no immediate terminal progression |
| Multiple allocations complicate picker contention | medium | deterministic per-allocation operation identity |

## 16. Acceptance / exit criteria

- [ ] PC3 finite-resource/contention/time gates pass.
- [ ] PC4 restart/replay/reconciliation/fault gates pass.
- [ ] PC5 observability/documentation gates pass.
- [ ] Existing inventory/replay invariants remain green.
- [ ] No backend-private handle becomes durable truth.
- [ ] Manifest reports PC5 and exact PC6 gaps.

## 17. Change history

| Date | Change | Rationale |
|---|---|---|
| 2026-10-08 | Initial draft | Technical design for PRD-0002 |
