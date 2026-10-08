# PRD-0002 — Warehouse Fulfillment PC5 Process Canonical

- **Status:** Accepted
- **Owner:** SOSE maintainers
- **Created:** 2026-10-08
- **Last updated:** 2026-10-08
- **Related TRDs:** TRD-0002
- **Related ADRs:** ADR-0001, ADR-0002
- **Related issues/PRs:** process audit #285

## 1. Problem

Warehouse Fulfillment is currently audited at PC2. It has durable allocation, picking, packing, shipping, immutable inventory occurrences, replay safety, and recurring reconciliation, but it does not yet model operational service capacity or elapsed processing/queue time.

Without those mechanics it cannot serve as a complete process canonical, participate credibly in the Trading Company composition, or support later Organizational Dynamics experiments without overstating operational realism.

## 2. Users and use cases

Primary users are domain authors, process-model reviewers, experiment authors, and operators composing Warehouse Fulfillment with Warehouse Management, Logistics, and Order-to-Cash.

Primary workflow:

1. allocate available inventory;
2. wait for constrained picking capacity;
3. complete picking after explicit processing time;
4. wait for constrained packing capacity;
5. complete packing after explicit processing time;
6. wait for constrained shipping-dock capacity;
7. ship after explicit processing time;
8. restart during queued/in-progress work without duplicate inventory effects;
9. observe canonical KPIs and projection state.

## 3. Goals

- promote Warehouse Fulfillment from PC2 to PC5;
- preserve inventory as business/material state rather than pretending it is service capacity;
- introduce explicit picker, packing-station, and shipping-dock resources;
- make queue wait and processing durations observable;
- preserve deterministic replay and immutable inventory occurrence semantics;
- prove continuous-vs-rebuild restart equivalence;
- provide PC5 KPIs, projection, ERD, StateCharts, process diagram, and configuration contract;
- keep standalone execution working.

## 4. Non-goals

- PC6 cross-domain composition;
- changing stock ownership for future composed mode;
- introducing a generic warehouse kernel abstraction;
- labor scheduling, multi-zone routing, wave planning, robotics, or optimization;
- Organizational Dynamics experiments before PC5/PC6 prerequisites are satisfied.

## 5. Requirements

### Functional

- **FR-1:** Picking shall require finite picker capacity.
- **FR-2:** Packing shall require finite packing-station capacity.
- **FR-3:** Shipping shall require finite shipping-dock capacity.
- **FR-4:** Resource contention shall delay work rather than bypass capacity.
- **FR-5:** Pick, pack, and ship processing times shall be explicit and configurable.
- **FR-6:** Interrupted work shall resume after restart without duplicating inventory decrement, occurrence evidence, or terminal transitions.
- **FR-7:** Continuous and rebuilt executions shall converge to equivalent durable operational state.
- **FR-8:** Fault/interruption recovery shall not leak resource demands, reservations, release intents, commands, or scheduled work.
- **FR-9:** A read-only projection shall expose current order/allocation/inventory/service state.
- **FR-10:** KPIs shall distinguish material shortage from service-capacity delay.
- **FR-11:** Configuration shall expose capacities and service durations with validation.
- **FR-12:** Existing substitution, all-or-nothing allocation, correction, and replay invariants shall remain valid.

### Quality / operational

- **QR-1:** Equal initial state/config/seed/input remains deterministic.
- **QR-2:** No backend-private SimPy object becomes durable truth.
- **QR-3:** Service-resource ownership is explicit and leak-free.
- **QR-4:** PC5 promotion is evidence-backed in ProcessManifest.
- **QR-5:** Projection functions are read-only and idempotent.
- **QR-6:** No cross-domain ownership semantics are introduced before PC6.

## 6. Success metrics

| Metric | Baseline | Target | Measurement |
|---|---:|---:|---|
| Process maturity | PC2 | PC5 | executable manifest |
| Finite service resources | 0 | 3 | resource definitions/tests |
| Contention paths | none | picker/pack/dock | integration tests |
| Restart equivalence | recovery only | continuous = rebuilt | e2e test |
| PC5 evidence gaps | multiple | 0 | manifest |
| Duplicate inventory effect after restart | risk | 0 | restart/idempotence tests |

## 7. Constraints

- Inventory quantity remains business/material truth, not Resource capacity.
- Inventory decrements remain protected by deterministic immutable occurrence identity.
- StateCharts remain the only legal lifecycle authority.
- Durable time semantics use SOSE scheduler/resource persistence contracts.
- Standalone behavior remains supported after operationalization.

## 8. User / system workflow

~~~mermaid
flowchart TD
  A[Requested order] --> B[Allocate inventory]
  B --> C[Wait for picker]
  C --> D[Pick processing]
  D --> E[Wait for packing station]
  E --> F[Pack processing]
  F --> G[Wait for shipping dock]
  G --> H[Ship processing]
  H --> I[Shipped]
~~~

## 9. Acceptance criteria

- [ ] Picker, packing-station, and shipping-dock resources are finite.
- [ ] At least one contention test proves queue delay.
- [ ] Pick/pack/ship durations are explicit and configurable.
- [ ] Existing inventory invariants remain green.
- [ ] Restart equivalence covers queued/in-progress operational boundaries.
- [ ] Resource/schedule cleanup is leak-free after success and recovery.
- [ ] Projection/KPI contract is executable and read-only.
- [ ] ERD, complete StateCharts, Mermaid process diagram, and configuration contract are normative.
- [ ] warehouse_fulfillment reports PC5 with PC6 as the next unmet gate.

## 10. Rollout expectations

Implementation is split into two technical increments:

1. PC3/PC4 operational durability: resources, contention, time, restart equivalence, recovery.
2. PC5 observability/documentation: projection, KPIs, normative specification, manifest promotion.

## 11. Risks and open questions

| Item | Impact | Resolution |
|---|---|---|
| Resource capacity held incorrectly across durable delay | high | TRD-0002 defines request/complete/release lifecycle and restart cases |
| Inventory decrement moves to wrong temporal boundary | high | preserve decrement at durable pick completion and deterministic occurrence identity |
| Auto-progress collapses stages in one tick | medium | completion occurs through durable scheduled semantics |
| Added resources leak into generic kernel abstractions | medium | keep names/mechanics domain-owned until repetition proves reuse |

## 12. Change history

| Date | Change | Rationale |
|---|---|---|
| 2026-10-08 | Initial draft | Plan audited PC2→PC5 promotion |
