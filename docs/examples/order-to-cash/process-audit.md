# Order-to-Cash — Process-Canonical Audit

## Result

Current audited maturity: **PC4 — Durable**.

The executable slice spans submitted sales demand through credit, fulfillment,
shipping, invoicing, receivable aging, collection, and a terminal collected
receivable. The audit treats SalesOrder, Receivable, and CollectionCase as
separate durable business responsibilities connected by deterministic identity
and causal metadata.

## PC0–PC4 evidence

The current implementation proves the cumulative gates through PC4:

- executable entities and StateCharts for SalesOrder, Receivable, and
  CollectionCase;
- command/event progression from submitted order through invoicing and
  collection;
- an end-to-end recurring job that reaches Receivable(`collected`);
- sad paths for credit hold, fulfillment-capacity loss, explicit partial
  fulfillment, overdue collection, and resource contention;
- finite `fulfillment_team` and `collection_agent` resources;
- durable queued resource demand that survives restart;
- durable due, overdue, and promise-follow-up ScheduledWork;
- restart recovery for missing Receivable and CollectionCase creation;
- restart recovery for pending fulfillment and collection capacity;
- continuous-vs-rebuild collection-path equivalence with the complete relevant
  durable snapshot compared at the terminal boundary;
- explicit replay idempotence for terminal receivable/case materialization,
  due scheduling, and repeated collection;
- recurring `SimulationJob` reconciliation through the due boundary to
  collection.

## Why the process is PC4 rather than PC3

The older reference suite already tested useful restart boundaries, but a
process-canonical `RESTART_EQUIVALENCE` claim is stronger than simply showing
that one callback can resume. The W1 audit therefore adds an architectural gate
that runs the same overdue/promise/collection path continuously and with a
runtime rebuild before the collection follow-up, then compares:

- SalesOrder;
- Receivable;
- CollectionCase;
- DomainEvents;
- ScheduledWork;
- SimulationPosition;
- scenario state;
- resource definitions, demands, reservations, and release intents.

The rebuilt path must end exactly as the continuous path: order invoiced,
receivable collected, collection case resolved, no pending schedule, and no
resource ownership left behind.

Replay safety is also verified separately. Repeating the terminal business
operations after collection must leave the durable snapshot unchanged.

## PC5 evidence

No PC5 requirement is credited by the current audit.

The existing `specification.md` is useful architectural prose, but the PC5 gate
requires explicit, reconstructable, consumer-facing contracts rather than
nearby information. In particular, the current document does not provide the
complete PC5 artifact set.

## Exact PC5 gaps

The current codebase snapshot is missing audited evidence for all six PC5
requirements:

1. `KPIS`
   - no executable KPI/SLA projection is defined;
   - plausible candidates such as order cycle time, fulfillment wait, DSO-like
     aging, overdue rate, and collection latency are not credited until defined
     and tested.

2. `ERD`
   - ownership is described in prose, but no normative persistent business and
     operational ERD is provided.

3. `STATECHART_DOCUMENTATION`
   - executable StateCharts exist, but the specification does not contain a
     complete Mermaid topology/transition contract for SalesOrder, Receivable,
     and CollectionCase.

4. `PROCESS_DIAGRAM`
   - the end-to-end and exception flows are textual rather than a normative
     Mermaid process diagram.

5. `PROJECTION_CONTRACT`
   - there is no named stable projection boundary for analytics, process mining,
     control-room, or digital-twin consumers.

6. `CONFIGURATION_DOCUMENTATION`
   - `OrderToCashConfig` exists and is validated, but its fields, units,
     mutability, and operational consequences are not documented as a process
     contract.

Therefore the next maturity gate is PC5 with the full six-item observability
contract still to be implemented.

## Audit discipline

This classification deliberately keeps adjacent concepts separate:

- resource definitions do not prove contention; queued demand is tested;
- restart recovery does not prove restart equivalence; a continuous baseline is
  compared against a rebuilt execution;
- deterministic helper IDs do not prove replay safety; terminal operations are
  replayed and compared;
- a Pydantic configuration model is not configuration documentation;
- executable StateCharts are not StateChart documentation;
- narrative ownership is not an ERD;
- persistence records are not a projection contract;
- plausible business measures are not KPIs until they have an executable
  definition.
