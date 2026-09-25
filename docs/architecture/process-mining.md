# Process Mining

## Purpose

SOSE should produce operational traces that are usable for process mining by construction.

A synthetic operational system already knows:

- entities;
- transitions;
- logical timestamps;
- causation;
- correlation;
- resources;
- scenarios.

That information should not be discarded and reconstructed later from unrelated tables.

---

## Canonical process event

Every domain-visible lifecycle transition should be representable as a canonical process event.

Recommended fields:

```text
event_id
case_id
activity
occurred_at
entity_type
entity_id
source_state
target_state
resource_id
causation_id
correlation_id
attributes
```

---

## Case identity

A process case identifies one process instance.

Possible case strategies:

```text
entity id
correlation id
explicit domain case id
derived composite key
```

Examples:

```text
maintenance case → work_order_id
order-to-cash case → order_id
procure-to-pay case → purchase_requisition correlation
claims case → claim_id
```

The domain pack owns case semantics.

---

## Activity

Activities may derive from:

```text
domain events
transition events
commands
source records
```

The preferred default is domain-visible events or StateChart transitions.

Internal engine events should not automatically become process activities.

---

## Transition log

SOSE should maintain a canonical transition representation.

Example:

```text
case_id = WO-001
activity = wait_for_material
source_state = RELEASED
target_state = WAITING_MATERIAL
occurred_at = 2027-01-04T10:00:00
```

This creates a process-mining-ready event stream.

---

## Causality

Process mining usually relies on case and time ordering.

SOSE has additional information:

```text
causation_id
correlation_id
```

This enables richer analysis than timestamp ordering alone.

Example:

```text
MaterialShortageDetected
        ↓
PurchaseRequisitionCreated
```

The relationship is known because SOSE generated it.

---

## Resource information

When resources are involved, process events should optionally preserve:

```text
resource_id
resource_type
resource_pool
```

This supports:

```text
handover analysis
resource utilization
bottleneck analysis
organizational mining
```

---

## StateChart semantics

StateCharts provide a normative process model.

Observed synthetic traces provide executed behavior.

Therefore SOSE naturally supports comparison between:

```text
declared legal model
        vs
observed simulation trace
```

This can enable synthetic conformance-checking experiments.

---

## Expected outputs

SOSE may expose three standard analytical products.

### Process event

One row per activity occurrence.

```text
process_event
```

### Case summary

One row per case.

Possible fields:

```text
case_id
started_at
completed_at
duration
event_count
current_state
terminal_state
scenario_count
```

### Transition summary

Aggregated edge statistics.

Possible fields:

```text
source_state
target_state
event
transition_count
avg_duration
p50_duration
p95_duration
```

---

## Durations

Transition durations require careful semantics.

Possible durations include:

```text
time since previous event
time in previous state
time between causal events
business-time duration
wall logical-time duration
```

SOSE should label duration semantics explicitly.

---

## Scenario context

Synthetic process data becomes especially useful when scenario context is attached.

Example:

```text
case = PO-100
scenario = supplier_disruption
transition = SENT → RECEIVED
duration = 72h
```

This makes scenario impact measurable.

Scenario metadata may be embedded in event attributes, joined through scenario activation tables, or kept simulator-private depending on the source contract.

---

## Probabilistic decision context

SOSE may optionally expose internal decision traces for model diagnostics.

Example:

```text
enabled transitions:
  ship = 0.60
  wait = 0.35
  cancel = 0.05

selected:
  wait
```

These traces are useful for simulation analysis but should remain distinct from ordinary process events.

---

## Export formats

Potential exports:

```text
CSV
Parquet
Delta
XES
OCEL
```

OCEL is especially relevant when one event relates to multiple objects.

Example:

```text
GoodsReceipt
    ↔ PurchaseOrder
    ↔ Material
    ↔ Warehouse
```

Traditional single-case event logs may lose this object-centric structure.

SOSE should preserve enough relationships to support object-centric process mining later.

---

## Object-centric process mining

Many operational events touch several entities simultaneously.

Example:

```text
Payment
    ↔ Invoice
    ↔ Supplier
    ↔ BankAccount
```

SOSE's explicit domain graph is well suited to an object-centric event model.

Future support should consider:

```text
event
  ├── object A
  ├── object B
  └── object C
```

instead of forcing every event into one case identifier.

---

## Invariants

1. Domain-visible transitions can be converted into canonical process events.
2. Case identity is domain-defined.
3. Event ordering uses logical simulation time and deterministic sequence.
4. Causal metadata remains available.
5. Internal engine events are not automatically process activities.
6. Resource context is preserved when relevant.
7. StateChart topology and observed traces remain separate artifacts.
8. Multi-object relationships are not destroyed merely to fit a flat case log.

---

## Future work

- XES export;
- OCEL export;
- conformance checking;
- directly-follows graphs;
- variant analysis;
- bottleneck reports;
- process simulation calibration;
- statechart-to-process-model conversion;
- synthetic process benchmark packs.
