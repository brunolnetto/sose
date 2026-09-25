# Source Interface

## Purpose

SOSE simulates operational systems.

Downstream consumers should observe the simulation through interfaces resembling real source systems rather than through SOSE's internal implementation details.

The Source Interface defines this boundary.

---

## Core principle

```text
simulation implementation
        ≠
source-system contract
```

SOSE may internally store:

```text
simulation tick
decision trace
scenario state
scheduler metadata
replay sequence
probabilistic weights
```

A simulated ERP table does not automatically expose those fields.

---

## Architectural position

```text
SOSE Runtime
     ↓
Persistence
     ↓
Source Interface
     ↓
Consumers
```

Potential consumers:

```text
ETL / ELT
CDC pipelines
analytics
stream processing
APIs
BI
ML
data-quality systems
```

---

## Interface types

SOSE may expose operational data through:

```text
SQL tables
database views
CDC
event streams
REST APIs
files
message queues
```

A domain pack may support one or multiple source interfaces.

---

## Operational state interface

Stateful entities may be exposed as current-state records.

Example:

```text
purchase_order
--------------
purchase_order_id
supplier_id
status
created_at
updated_at
currency
total_amount
```

SOSE-private fields remain hidden unless explicitly enabled.

---

## Event interface

Operational event tables may expose append-only facts.

Example:

```text
inventory_movement
------------------
movement_id
material_id
warehouse_id
movement_type
quantity
occurred_at
```

This is distinct from the internal SOSE domain-event log.

A domain event such as:

```text
PurchaseOrderReceived
```

does not automatically imply that an external table with the same shape exists.

The domain pack defines source representation.

---

## CDC interface

SOSE can provide a highly useful data-engineering test surface by exposing change streams.

CDC semantics should model:

```text
insert
update
delete
commit order
commit timestamp
transaction boundaries
```

Potential adapters include:

```text
Delta Change Data Feed
PostgreSQL logical decoding
Debezium-compatible source
synthetic CDC log
```

---

## Time semantics

The source interface should preserve distinctions among:

```text
business/event time
source commit time
consumer ingestion time
```

SOSE owns the first two.

The downstream system typically owns ingestion time.

Example:

```text
occurred_at              10:15:00
source_commit_timestamp  10:15:04
bronze_ingested_at       10:20:31
```

The third timestamp does not belong to the source simulation unless SOSE is also simulating the consumer.

---

## Source commits

SOSE may execute many internal operations during one simulation tick.

The source interface should expose a coherent commit boundary.

Possible model:

```text
simulation transaction
        ↓
source mutations
        ↓
source commit
        ↓
CDC records become visible
```

External consumers must not rely on SOSE's private `committed_tick`.

They should observe only source-visible commits.

---

## Schema evolution

Real source systems evolve.

SOSE should eventually support controlled source schema changes:

```text
add column
change optionality
new enum value
new source entity
deprecated field
```

This allows testing:

- ingestion resilience;
- contracts;
- schema evolution;
- backward compatibility.

---

## Imperfect sources

A useful source simulator should eventually support controlled source imperfections.

Examples:

```text
late commits
duplicate records
missing optional values
out-of-order events
temporary reconciliation mismatch
corrective updates
soft deletes
```

These should be explicit scenarios or source-adapter behaviors, not accidental bugs.

---

## Multiple source systems

A domain may expose more than one synthetic source.

Example MRO:

```text
maintenance system
inventory system
procurement ERP
finance system
```

The same SOSE simulation may drive all four while preserving separate:

```text
schemas
commit boundaries
latencies
source identifiers
```

This creates realistic cross-system integration behavior.

---

## Source ownership

Domain packs define:

```text
which entities are visible
which fields are visible
how state maps to records
which events become source facts
which mutations generate CDC
```

The SOSE kernel should not infer operational schemas automatically from Python object internals.

---

## Invariants

1. Simulator-private metadata is hidden by default.
2. Source-visible identity remains stable.
3. Source commits are ordered deterministically.
4. CDC derives from source-visible mutations.
5. External consumers do not depend on `committed_tick`.
6. Source schema is an explicit domain contract.
7. Internal domain events and external event tables remain separate concepts.
8. Source imperfections are deliberately modeled.

---

## Future work

- relational source adapter;
- Delta source adapter;
- CDC protocol;
- Kafka source interface;
- REST source emulator;
- source latency model;
- schema migration scenarios;
- multi-system transactional boundaries.
