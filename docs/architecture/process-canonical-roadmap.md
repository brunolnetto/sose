# Process Canonical Roadmap

## Status

Normative roadmap for promoting SOSE business examples from semantic reference slices into complete, durable, observable, and composable process simulations.

## Why this roadmap exists

SOSE already has broad domain coverage. The next architectural problem is not adding more sectors; it is making process depth consistent across the existing business-domain catalog.

The earlier `domain-frontier.md` remains useful for deciding whether a *new* sector adds genuinely new semantic pressure. This roadmap governs a different question:

> how far should an existing business domain be promoted from a reference slice into an end-to-end operational process canonical?

New sectors should therefore remain exceptional while the current catalog is audited and promoted.

## Frozen decisions

1. **Keep the kernel generic.** Business-process abstractions remain above the SOSE kernel unless repeated evidence proves that a mechanism is domain-independent.
2. **Promote processes, not nouns.** A mature business example must execute a meaningful trigger-to-outcome process, not merely expose entities and statecharts.
3. **Preserve standalone execution.** Every promoted domain remains runnable by itself.
4. **Add composed execution through explicit boundaries.** Cross-domain composition uses named ingress/egress contracts, causal identifiers, and durable events; one domain must not mutate another domain's private entities directly.
5. **One durable owner per business concept.** Integration references ownership rather than duplicating durable truth.
6. **Extract shared abstractions only after repetition.** Do not introduce `GenericOrder`, `GenericShipment`, `GenericEnterprise`, or equivalent abstractions merely because two domains use similar nouns.
7. **Use Warehouse Management as the first fully audited process reference, not as a presumed PC5.** Its current audit is allowed to expose lower-level gaps; Warehouse Fulfillment is the companion target reference and must also be audited before its maturity is claimed.
8. **Build the first integrated enterprise backbone before deepening every sector.** P2P, Warehouse, O2C, Logistics, Cards/Payments, and R2R form the first composition target.
9. **Organizational Dynamics and Digital Twin projections consume mature process canonicals.** They do not block process promotion.
10. **Promotion is evidence-backed.** TDD, durable recovery, replay safety, documentation, and executable evidence determine maturity; implementation reputation or file count does not.

## Maturity model

SOSE process maturity is represented by `PC0` through `PC6`.

| Level | Name | Meaning |
|---|---|---|
| PC0 | Registered | Domain is catalogued and has validated runtime/configuration construction. |
| PC1 | Behavioral | Entities, statecharts, command/event behavior, and an executable happy path exist. |
| PC2 | Process | A trigger reaches a durable terminal business outcome end-to-end. |
| PC3 | Operational | Sad paths, finite resources, capacity contention/queueing, and explicit time semantics are demonstrated. |
| PC4 | Durable | State is durable; restart equivalence, replay idempotence, recurring reconciliation, and fault recovery are demonstrated. |
| PC5 | Observable | KPIs, ERD, documented statecharts, E2E process diagram, projection boundary, and configuration contract exist. |
| PC6 | Composable | Stable ingress/egress contracts exist and at least one cross-domain execution path is tested. |

A **Complete Process Canonical** is PC5 or above.

An **Integrated Process Canonical** is PC6.

Maturity is cumulative. A domain cannot skip an incomplete lower gate.

## Evidence policy

`src/sose/examples/process_manifest.py` is the executable evidence registry for this roadmap.

A manifest does not infer capabilities from filenames. It records reviewed evidence.

If a domain has not yet been audited, it remains a conservative PC0 lower bound even if richer behavior exists in code. This distinction is intentional:

> missing audited evidence is not proof of missing implementation.

The audit process promotes evidence only after the relevant code, tests, and specification have been inspected.

Every non-PC0 evidence claim in a manifest with `assessment_complete=True` must be bound to one or more repository-relative provenance paths in `evidence_sources`. The model validates the provenance shape without filesystem access; repository tests verify that those paths continue to exist. A test, implementation file, or specification may support more than one claim when it actually contains the relevant evidence.

This provenance rule exists to prevent maturity from becoming a manually maintained score. Reviewers must be able to move from a maturity claim to the executable or documentary source that supports it.

Evidence above the current maturity gate may already exist. Because maturity is cumulative, a missing lower-gate requirement still blocks promotion. For example, documentation and KPIs do not compensate for a missing restart-equivalence test.

## PC5 completion contract

A process may be promoted to PC5 only when the following evidence is explicit and executable where applicable:

- trigger and end-to-end terminal business outcome;
- happy path;
- multiple meaningful sad paths;
- finite operational capacity;
- capacity contention or queueing semantics;
- explicit timing behavior;
- durable business state;
- restart/rebuild equivalence;
- replay-safe/idempotent business effects;
- recurring reconciliation or equivalent continuation semantics;
- fault-recovery behavior;
- domain KPIs / SLA projection;
- ERD;
- entity statechart documentation;
- Mermaid end-to-end process diagram;
- projection boundary for external UIs/analytics/digital twins;
- validated and documented configuration parameters.

A process is not PC5 merely because each entity has a statechart.

## PC6 composition contract

PC6 adds:

- named ingress contracts;
- named egress contracts;
- durable correlation/causal identity across boundaries;
- ownership preservation;
- at least one tested cross-domain path;
- restart-safe behavior across the composed path.

Cross-domain consumers reference another domain's outputs. They do not take ownership of another domain's durable internal state.

## Business concept ownership

Initial ownership guidance:

| Concept | Owner |
|---|---|
| Sales order, credit decision, receivable | Order-to-Cash |
| Purchase order, supplier obligation/invoice | Procure-to-Pay |
| Stock position, warehouse site, internal stock transfer | Warehouse Management |
| Fulfillment order, pick/pack/stage/dispatch | Warehouse Fulfillment |
| External transport shipment / transport leg | Logistics |
| Authorization, capture, payment clearing/settlement | Cards & Payments |
| Journal, reconciliation, accounting period close | Record-to-Report |
| Maintenance work order / maintenance execution | MRO |
| Production order / WIP / production execution | Manufacturing |
| Insurance policy / claim | Insurance |
| Loan / repayment schedule / delinquency | Credit & Loans |

These boundaries are hypotheses to be tested by composition work. Changing ownership requires an explicit architecture decision rather than accidental cross-domain mutation.

## Implementation waves

### W0 — Process canonical contract

Deliverables:

- this roadmap;
- dependency graph;
- executable PC0-PC6 maturity model;
- conservative built-in process audit.

Exit gate: the CI can report evidence-backed process maturity without guessing unaudited capability.

### W1 — Catalog-wide evidence audit

Inspect every built-in business domain and populate only evidence that is already supported by code/tests/docs.

W1 is split into evidence-provenance batches rather than one monolithic manual audit. Existing `ReferenceContract` provenance should be reused where it already supports a process claim; newer domains without reference-catalog coverage require direct inspection.

Exit gate:

- every domain has `assessment_complete=True` for the current codebase snapshot;
- every non-PC0 evidence claim has repository provenance;
- every PC gap is explicit;
- no domain is promoted based solely on intuition.

### W2 — Warehouse reference canonicals

Use Warehouse Management and Warehouse Fulfillment to establish the reference implementation style.

Warehouse Management is the first fully audited process reference. Its initial audit currently identifies `RESTART_EQUIVALENCE` as an unproven PC4 requirement, so its maturity remains PC3 until a continuous-vs-rebuild test is added.

Warehouse Fulfillment must be audited and then promoted using the same standard, including a normative `specification.md`, KPI/projection contract, and any missing durability/operational evidence found by W1.

Exit gate: both warehouse processes are PC5.

### W3 — Cross-domain boundary contract

Define the minimum generic composition mechanism without inventing a generic enterprise ontology.

Required semantics:

- boundary message identity;
- source domain;
- destination contract;
- correlation / causation;
- idempotent consumption;
- durable delivery/recovery semantics;
- explicit ownership.

Exit gate: two reference domains can exchange one durable contract without direct private-state mutation.

### W4 — Trading-company backbone PC5

Promote the domains required by the first enterprise composition:

1. Procure-to-Pay;
2. Order-to-Cash;
3. Logistics;
4. Cards & Payments;
5. Record-to-Report;
6. Warehouse Fulfillment;
7. Warehouse Management.

Exit gate: all are independently PC5.

### W5 — Trading-company composition PC6

Compose a minimal company flow:

Customer demand -> O2C -> Warehouse Fulfillment -> Logistics -> delivery -> payment -> R2R,
with replenishment flowing Warehouse Management -> P2P -> receiving -> Warehouse Management and accounting outputs flowing to R2R.

Exit gate:

- at least one customer-demand path executes across domain boundaries;
- at least one replenishment path executes across boundaries;
- restart/recovery does not duplicate cross-domain effects;
- business ownership remains unambiguous.

### W6 — Manufacturing and MRO

Promote Manufacturing and MRO to PC5, then compose them with Warehouse/P2P.

Target interactions:

- production material demand -> Warehouse -> P2P if unavailable;
- finished production -> Warehouse;
- equipment failure -> MRO;
- MRO spare requirement -> Warehouse/P2P;
- asset restored -> Manufacturing.

### W7 — Asset-intensive operations

Promote and compose:

- Construction;
- Field Service;
- Energy / Utilities;
- relevant Logistics / Warehouse / MRO dependencies.

### W8 — Mobility and service cluster

Promote and compose where justified:

- Aviation;
- Airports;
- Transit;
- Hospitality.

### W9 — Digital-service cluster

Promote and compose:

- Telecommunications;
- Subscription SaaS;
- ITSM.

### W10 — Regulated-finance cluster

Promote and compose:

- Insurance;
- Credit & Loans;
- Cards & Payments;
- R2R where accounting integration is meaningful.

### W11 — Healthcare

Promote Hospitals to a complete process canonical, retaining healthcare-specific capacity and priority semantics rather than forcing it into the trading-company model.

### W12 — Organizational Dynamics experiments

Only after credible PC5/PC6 processes exist, use them as experimental systems for:

- capacity interventions;
- policy interventions;
- structural interventions;
- actor agency A0/A1/A2;
- break-even intervention analysis;
- regime discovery.

### W13 — Digital Twin projections

Project durable process state into:

- dashboards;
- process-mining views;
- operational control rooms;
- 2D UIs;
- 3D React/Three-Fiber-style digital twins;
- replay visualizations.

Projection remains downstream of durable truth.

## Change policy

Until W5 is complete:

- adding a new sector is lower priority than closing an existing process-completeness gap unless it exposes genuinely new kernel semantics;
- maturity claims must be expressed through the executable manifest;
- process composition must not introduce a generic enterprise super-model;
- shared abstractions are extracted only after concrete duplication appears;
- each wave is implemented TDD-first and merged only when the complete CI gate is green.

## Immediate next step

After W0 merges, run W1 as an evidence audit of the current catalog. The output of that audit, not intuition, selects the exact sequence of PC-gap PRs required to reach W2 and W4.
