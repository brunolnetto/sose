# Procure-to-Pay — Process-Canonical Audit

## Result

Current audited maturity: **PC5 — Observable**.

This audit applies to the executable operational P2P slice described by
`specification.md`: internal requisition through supplier lead time, receiving,
stocking, allocation, and material consumption. It deliberately does not expand the
reference into supplier invoicing, accounts payable, or payment settlement.

## PC0–PC4 evidence

The implementation retains the cumulative evidence through PC4:

- entities and executable StateCharts for Requisition, PurchaseOrder, Receipt, and
  MaterialDemand;
- command/event happy path from requested requisition to consumed material;
- receiving contention, shortage/backorder, partial receipt, and rejected-receipt
  sad paths;
- finite `receiving_dock` and `inspector` resources and durable contention;
- durable supplier lead time through ScheduledWork;
- durable Store + Container inventory effects;
- continuous-vs-multi-restart semantic equivalence across entity, event, schedule,
  scenario, resource, Store, Container, and simulation-position truth;
- replay-idempotent stocking and consumption;
- recurring reconciliation and fault-recovery evidence.

## PC5 evidence

All six PC5 requirements now have explicit executable/documented provenance.

### KPIs

`p2p_kpis()` defines and tests:

- `procure_to_consumption_seconds`;
- `quantity`;
- `transition_count`;
- `supplier_delay_count`;
- `partial_receipt_count`;
- `rejected_receipt_count`;
- `backorder_count`;
- `consumed`.

Exception-history KPIs are counted from immutable correlated transition events, not
inferred from final entity state.

### ERD

`specification.md` retains the normative business and durable-operational Mermaid
ERDs and explicitly distinguishes semantic relationships from fabricated physical
foreign keys.

### StateChart documentation

The specification now mirrors all four executable StateCharts, including the
PurchaseOrder and MaterialDemand cancellation states/transitions that were previously
described only in prose.

### Process diagram

The happy path and representative receiving, shortage, partial, and rejection flows are
normative Mermaid diagrams.

### Projection contract

`p2p_projection()` provides a stable read-only projection over the four durable
business entities plus the durable inventory Container. It does not fabricate financial
P2P concepts outside the executable operational slice.

### Configuration documentation

The specification documents every `P2PConfig` field, constraints/meaning, and runtime
mutability, aligned with `DomainDefinition.runtime_mutable_fields`.

## Promotion decision

Procure-to-Pay is promoted from **PC4 — Durable** to **PC5 — Observable**.

The next maturity gate is **PC6 — Composable** and remains unmet until explicit ingress
contracts, egress contracts, and cross-domain execution evidence exist.

This promotion is a process-canonical qualification only. It does not authorize an
official Organizational Dynamics experiment. The accepted SOSE 1.0 program separately
requires domain-specific accepted PRD/TRD plus a hash-frozen preregistration before any
official experiment execution/evidence.

## Audit discipline

The audit keeps neighboring concepts separate:

- resource definitions do not prove contention;
- restart recovery does not prove restart equivalence;
- deterministic identities do not prove replay idempotence;
- executable StateCharts do not count as documentation until the normative topology is
  complete;
- a Pydantic configuration model is not configuration documentation;
- persistence records are not automatically a consumer projection;
- plausible business measures are not KPIs until executable definitions/tests exist;
- PC5 completeness does not imply PC6 integration.
