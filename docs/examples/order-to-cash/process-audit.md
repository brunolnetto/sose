# Order-to-Cash — Process-Canonical Audit

## Result

Current audited maturity: **PC5 — Observable**.

The executable slice spans submitted sales demand through credit, fulfillment,
shipping, invoicing, receivable aging, collection, and a terminal collected
receivable. SalesOrder, Receivable, and CollectionCase remain separate durable
business responsibilities connected by deterministic identity, correlation, and
causation.

## PC0–PC4 evidence

The implementation retains the cumulative evidence through PC4:

- executable entities and StateCharts for SalesOrder, Receivable, and CollectionCase;
- command/event progression from submitted order through invoicing and collection;
- recurring execution reaching Receivable(`collected`);
- credit hold, fulfillment-capacity loss, partial fulfillment, overdue collection,
  and resource-contention sad paths;
- finite `fulfillment_team` and `collection_agent` resources;
- durable due, overdue, and promise-follow-up ScheduledWork;
- restart recovery/equivalence across causal creation, schedules, and resource demand;
- replay idempotence for terminal materialization, scheduling, and collection.

## PC5 evidence

The PC5 observability contract is now explicit and reconstructable.

### KPIs

`order_to_cash_kpis()` defines and tests:

- `cash_collection`;
- `order_to_cash_seconds`;
- `amount`;
- `transition_count`;
- `credit_hold_count`;
- `partial_fulfillment_count`;
- `overdue_count`;
- `collection_case_count`;
- `collection_escalation_count`.

The KPI contract is read-only and derived from persisted entities plus immutable
correlated transition events.

### ERD

`specification.md` contains a normative Mermaid ERD for SalesOrder → Receivable →
optional CollectionCase and explicitly distinguishes semantic relationships from SQL
foreign-key implementation.

### StateChart documentation

`specification.md` contains complete Mermaid StateCharts that mirror the executable
SalesOrder, Receivable, and CollectionCase topologies.

### Process diagram

`specification.md` contains a normative Mermaid end-to-end process from credit
decision through fulfillment, invoicing, due/overdue handling, collection, and case
resolution.

### Projection contract

`order_to_cash_projection()` provides a named stable read-only projection for
consumer-facing state. Absent causally-not-yet-created Receivable/CollectionCase
records are represented as null rather than invented entities, and terminal
order-to-cash duration remains null until collection is durable.

### Configuration documentation

`specification.md` documents every `OrderToCashConfig` field, constraints,
operational meaning, and runtime mutability. The mutable set is tied to
`DomainDefinition.runtime_mutable_fields`.

## Promotion decision

Order-to-Cash is promoted from **PC4 — Durable** to **PC5 — Observable**.

The promotion is based on executable KPI/projection evidence plus normative ERD,
StateChart, process-flow, and configuration contracts. No evidence class is credited
from nearby prose alone.

This promotion qualifies the process canonical for the next experiment-planning gate;
it does **not** itself authorize an official organizational experiment. The accepted
multi-domain program still requires a domain-specific PRD/TRD and a hash-frozen
preregistration protocol before official experiment execution/evidence.

## Audit discipline

The following distinctions remain enforced:

- resource definitions do not prove contention;
- restart recovery does not imply restart equivalence without comparison;
- deterministic IDs do not imply replay safety without replay evidence;
- executable StateCharts are not documentation unless the normative topology is
  published;
- narrative ownership is not an ERD;
- persistence records are not a projection contract;
- plausible measures are not KPIs until executable definitions and tests exist.
