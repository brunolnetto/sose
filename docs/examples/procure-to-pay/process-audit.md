# Procure-to-Pay — Process-Canonical Audit

## Result

Current audited maturity: **PC4 — Durable**.

This audit applies to the executable operational P2P slice described by
`specification.md`: internal requisition through supplier lead time, receiving,
stocking, allocation, and material consumption. It does not expand the example
into supplier invoicing, accounts payable, or payment settlement.

## PC0–PC4 evidence

The current implementation proves the cumulative gates through PC4:

- entities and executable StateCharts for requisition, purchase order, receipt,
  and material demand;
- command/event happy path from requested requisition to consumed material;
- explicit sad paths for receiving contention, shortage/backorder, partial
  receipt, and rejected receipt;
- finite operational resources `receiving_dock` and `inspector`;
- durable resource contention/queueing;
- durable supplier lead-time semantics through ScheduledWork;
- Store + Container durable inventory effects;
- continuous-vs-multi-restart semantic equivalence, including resource, Store,
  Container, schedule, scenario, event, and simulation-position state;
- explicit terminal replay idempotence for stocking and consumption;
- recurring `SimulationJob` reconciliation across supplier lead time through
  material consumption;
- recovery after a partially committed inventory effect and across receipt
  exception restart boundaries.

## PC5 evidence already present

Three observable-documentation requirements are already supported:

- `ERD` — business and durable operational ERDs are present in `specification.md`;
- `STATECHART_DOCUMENTATION` — the executable transitions are represented in
  Mermaid state diagrams and transition tables;
- `PROCESS_DIAGRAM` — the happy path and representative sad paths are represented
  as Mermaid process flows.

These do not permit promotion to PC5 because maturity is cumulative within the
PC5 gate itself.

## Exact PC5 gaps

The current codebase snapshot does **not** provide audited evidence for:

1. `KPIS`
   - no process KPI/SLA projection is defined and tested;
   - examples could eventually include procurement lead time, receiving wait,
     supplier-delay exposure, backorder duration, or first-pass receipt rate,
     but the audit does not invent metrics.

2. `PROJECTION_CONTRACT`
   - durable process truth exists, but there is no named stable projection
     boundary for analytics, process mining, control-room, or digital-twin
     consumers.

3. `CONFIGURATION_DOCUMENTATION`
   - `P2PConfig` is validated code and the runtime consumes its parameters, but
     the process specification does not yet document the configuration surface,
     semantics, units, mutability, and operational consequences as a contract.

Therefore the next maturity gate is exactly PC5 with those three missing claims.

## Audit discipline

The audit deliberately distinguishes nearby but non-equivalent evidence:

- having resource definitions is not enough; contention is separately tested;
- having a rebuild test is not enough; restart equivalence uses a continuous
  baseline and compares complete durable snapshots;
- idempotent-looking code is not enough; terminal stocking and consumption are
  explicitly replayed and required to leave the durable snapshot unchanged;
- having a Pydantic configuration model is not configuration documentation;
- having rich persistence state is not a projection contract;
- plausible operational measures are not KPIs until they are defined and
  executable.
