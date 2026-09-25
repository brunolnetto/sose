# Causality and Correlation

## Purpose

SOSE-generated data should preserve not only **what happened**, but also **why events are related**.

Operational systems contain causal chains.

Example:

```text
Material shortage detected
        ↓
Create purchase requisition
        ↓
Purchase requisition created
        ↓
Approve requisition
        ↓
Purchase order created
        ↓
Goods received
        ↓
Stock replenished
```

If SOSE generates these records independently, the resulting dataset may look realistic statistically while remaining operationally incoherent.

SOSE therefore treats causality and correlation as first-class concepts.

---

## Core distinction

### Causation

Causation links an object to its immediate trigger.

```text
A caused B
```

Represented by:

```text
causation_id
```

### Correlation

Correlation groups a broader business flow.

```text
A, B, C, D belong to the same operational journey
```

Represented by:

```text
correlation_id
```

These concepts must not be conflated.

---

## Example

Suppose a maintenance shortage produces a procurement flow.

```text
E1 MaterialShortageDetected
        ↓
C1 CreatePurchaseRequisition
        ↓
E2 PurchaseRequisitionCreated
        ↓
C2 ApprovePurchaseRequisition
        ↓
E3 PurchaseRequisitionApproved
```

Possible metadata:

```text
E1
  id = E1
  causation_id = null
  correlation_id = FLOW-123

C1
  id = C1
  causation_id = E1
  correlation_id = FLOW-123

E2
  id = E2
  causation_id = C1
  correlation_id = FLOW-123

C2
  id = C2
  causation_id = E2
  correlation_id = FLOW-123

E3
  id = E3
  causation_id = C2
  correlation_id = FLOW-123
```

This allows both immediate and end-to-end reconstruction.

---

## Command and event chains

Commands express intent. Events represent facts.

The causal lifecycle is:

```text
Domain Event
     ↓
Command
     ↓
StateChart transition
     ↓
Domain Event
     ↓
Command
     ↓
...
```

SOSE factories should propagate causality automatically where possible.

```python
command = ctx.commands.create(
    "purchase_requisition.approve",
    target=requisition,
    caused_by=created_event,
)
```

Then:

```python
approved = ctx.events.create(
    "purchase_requisition.approved",
    entity=requisition,
    caused_by=command,
)
```

---

## Root correlation

When no previous correlation exists, SOSE should establish one.

```text
new independent business flow
    → create correlation_id

caused object already correlated
    → inherit correlation_id
```

The exact correlation strategy may be domain-configurable.

---

## Cross-entity causality

A StateChart owns an entity lifecycle.

Cross-entity behavior occurs through:

```text
event
  ↓
scenario / reaction / handler
  ↓
command
  ↓
another entity
```

Example:

```text
WorkOrder
   ↓ shortage
MaterialRequirement
   ↓
PurchaseRequisition
   ↓
PurchaseOrder
   ↓
GoodsReceipt
   ↓
InventoryMovement
```

---

## Correlation is not statistical correlation

`correlation_id` means operational relationship.

It does **not** mean Pearson correlation, statistical dependence, or causal inference.

---

## Causal graph

The event/command history naturally forms a directed graph.

```text
E1
├── C1
│   └── E2
│       ├── C2
│       │   └── E3
│       └── C3
│           └── E4
```

This structure enables:

- root-cause tracing;
- workflow reconstruction;
- process mining;
- debugging;
- simulation explanation;
- lineage between synthetic entities.

---

## Forks

One cause may generate multiple effects.

```text
OrderPaid
   ├── ReserveInventory
   ├── ScheduleShipment
   └── RecordRevenue
```

All descendants may share one correlation ID while preserving distinct causation links.

---

## Joins

A downstream event may depend on multiple upstream facts.

A single `causation_id` is insufficient for true multi-cause semantics.

SOSE should eventually support:

```text
causation_ids = [...]
```

or a separate causal-edge representation.

---

## Temporal causality

Cause and effect should usually respect logical-time ordering:

```text
cause.occurred_at <= effect.occurred_at
```

Exceptions such as late-arriving records or corrected timestamps should be explicit.

---

## Scenario causality

Scenario activations may participate in causal history.

```text
SupplierDisruptionActivated
        ↓
PurchaseOrderDelayed
        ↓
MaterialShortageDetected
```

This makes simulated shocks explainable.

---

## Process mining

A process case can often be derived from:

```text
correlation_id
```

while activities come from domain events and state-transition events.

---

## Invariants

1. Every command and event has a stable identity.
2. `causation_id` identifies the immediate trigger when one exists.
3. `correlation_id` identifies the broader operational flow.
4. Factories propagate correlation through causal chains.
5. Cross-entity reactions preserve causal metadata.
6. Scenario-driven effects may preserve scenario causality.
7. Correlation metadata does not replace explicit domain relationships.
8. Causal metadata remains deterministic under replay.

---

## Anti-patterns

Avoid one global correlation ID for an entire simulation.

Avoid setting:

```text
causation_id = correlation_id
```

by default.

Avoid inventing causal edges merely because two events occur close in time.

SOSE causality is generated from the simulation execution graph, not inferred afterward.

---

## Future work

- multi-parent causality;
- causal graph queries;
- causal trace visualization;
- root-cause reports;
- configurable correlation boundaries;
- OpenTelemetry-style trace export;
- W3C trace-context adapters;
- process-case derivation policies.
