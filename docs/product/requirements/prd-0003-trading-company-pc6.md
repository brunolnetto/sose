# PRD-0003 — Trading Company PC6 Composition

- **Status:** Accepted
- **Owner:** SOSE maintainers
- **Created:** 2026-10-08
- **Last updated:** 2026-10-08
- **Related TRDs:** TRD-0003
- **Related ADRs:** ADR-0006
- **Related issues/PRs:** process-canonical roadmap W5

## 1. Problem

SOSE has several PC5 process canonicals, but they are qualified primarily as standalone
processes. The SOSE 1.0 organizational experiment program requires a credible PC6
composition before any built-in pilot produces official Organizational Dynamics evidence.

The first PC6 composition must prove that independent domains can exchange durable
business contracts without sharing or mutating private state, while preserving
correlation, causation, idempotence, restart safety, and business ownership.

## 2. Users and use cases

The reference use case is a synthetic trading company with two required composed flows:

~~~mermaid
flowchart LR
    D[Customer demand] --> O2C[Order-to-Cash]
    O2C --> WF[Warehouse Fulfillment]
    WF --> L[Logistics]
    L --> O2C
    O2C --> P[Cards / Payments]
    P --> O2C
    O2C --> R2R[Record-to-Report]
    P --> R2R

    WM[Warehouse Management] --> P2P[Procure-to-Pay]
    P2P --> WM
    P2P --> R2R
~~~

## 3. Goals

- establish the first credible PC6 composition in SOSE;
- define stable named ingress/egress contracts between participating domains;
- prove durable causal/correlation identity across domain boundaries;
- prove idempotent consumption and retry without duplicate business effects;
- prove continuous-vs-restart semantic equivalence across composed paths;
- preserve one durable owner for every business concept;
- keep each PC5 domain independently executable;
- provide executable provenance for PC6 maturity claims.

## 4. Non-goals

- a generic enterprise ontology;
- distributed microservices or network transport;
- cross-database transactions between domains;
- promotion of shared abstractions before concrete duplication exists;
- Organizational Dynamics experiment evidence;
- changing ownership of existing business concepts;
- modeling every possible enterprise integration edge.

## 5. Requirements

### Functional

- **FR-1:** Every boundary message shall have deterministic identity, source domain,
  destination contract, correlation ID, causation ID, logical production time, and
  immutable payload.
- **FR-2:** Producers own emitted business facts; consumers may not mutate producer
  private state.
- **FR-3:** Consumption shall be idempotent under duplicate delivery and restart.
- **FR-4:** A durable delivery intent shall survive process failure and be retryable.
- **FR-5:** Customer demand shall complete at least one composed path through
  O2C → Warehouse Fulfillment → Logistics → payment → accounting.
- **FR-6:** Replenishment shall complete at least one composed path through
  Warehouse Management → P2P → inventory receipt → Warehouse Management.
- **FR-7:** Accounting consequences from P2P, O2C, and Payments shall be consumable
  by R2R without transferring source-domain ownership.
- **FR-8:** Cross-domain correlation/causation shall survive persistence/restart.
- **FR-9:** Recovery shall be safe after failure between emit, delivery, consume,
  and acknowledgement boundaries.
- **FR-10:** Process manifests shall only claim PC6 when explicit ingress, egress,
  cross-domain execution, restart, and provenance evidence exist.
- **FR-11:** In composed mode, Warehouse Management shall remain the sole durable
  owner of stock position/reservation truth. Warehouse Fulfillment shall not create
  or mutate authoritative local InventoryLot stock; it shall consume WM availability/
  reservation contracts and retain only immutable foreign reservation/stock references.
- **FR-12:** Warehouse Fulfillment standalone mode shall retain its current local
  InventoryLot behavior so PC6 composition does not break the standalone reference.

### Quality / operational

- **QR-1:** Equal initial state/configuration/seed produces equal boundary identities
  and composed outcomes.
- **QR-2:** Continuous and rebuilt execution produce equivalent durable business and
  boundary-delivery state.
- **QR-3:** Duplicate delivery never duplicates a business effect.
- **QR-4:** No consumer requires direct access to another domain's private entity object.
- **QR-5:** Existing standalone domain tests remain green.
- **QR-6:** PC6 remains implementable without a generic enterprise super-model.

## 6. Success metrics

| Metric | Target | Evidence |
|---|---:|---|
| Customer-demand composed path | 1 complete path | E2E test |
| Replenishment composed path | 1 complete path | E2E test |
| Duplicate-delivery business effects | 0 | replay/idempotence tests |
| Restart divergence | 0 | continuous-vs-rebuild test |
| Private cross-domain mutation | 0 | ownership tests |
| PC6 provenance gaps | 0 | manifest audit |

## 7. Constraints

- business ownership follows the accepted process-canonical roadmap;
- durable operational truth remains in SOSE persistence;
- consumers receive immutable contract payloads, not mutable entity references;
- the first composition remains an in-process reference implementation;
- delivery semantics must remain portable across SQLite/PostgreSQL persistence;
- completing PC6 does not itself authorize an official organizational experiment.

## 8. Acceptance criteria

- [ ] Required customer-demand path reaches durable terminal outcomes across boundaries.
- [ ] Replenishment path crosses WM → P2P → WM without shared private state.
- [ ] Accounting outputs reach R2R through named contracts.
- [ ] Duplicate delivery is idempotent at each exercised boundary.
- [ ] Representative cross-domain crash/restart is continuous-equivalent.
- [ ] Correlation and causation survive restart.
- [ ] No domain mutates another domain's private state.
- [ ] Composed Warehouse Fulfillment uses WM-owned availability/reservation/consumption
  contracts and has no second authoritative stock projection.
- [ ] Standalone Warehouse Fulfillment retains the existing local InventoryLot path.
- [ ] PC6 evidence is provenance-bound in executable manifests.
- [ ] At least one resulting process canonical reaches PC6.
- [ ] Standalone domain execution remains supported.

## 9. Rollout expectations

1. boundary envelope + delivery/consumption semantics;
2. customer-demand path;
3. replenishment/accounting paths;
4. restart/replay/fencing tests;
5. projection/docs and PC6 audit/promotion.

## 10. Risks and open questions

| Item | Impact | Mitigation |
|---|---|---|
| Boundary abstraction becomes enterprise ontology | high | envelope contains transport metadata only |
| Consumer accidentally owns producer state | high | immutable payload + consumer command boundary |
| Publish-before-ack duplicates effects | high | deterministic consumption identity |
| Composition hides process maturity gaps | high | every participant independently PC5 |
| First graph is too broad | medium | implement only roadmap W5 paths |

## 11. Change history

| Date | Change | Rationale |
|---|---|---|
| 2026-10-08 | Initial draft | Plan W5 Trading Company PC6 |
