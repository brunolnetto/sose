# `docs/architecture/README.md`

# SOSE Architecture

This directory contains the architectural contracts of the **Synthetic Operational System Engine (SOSE)**.

SOSE models synthetic operational systems as deterministic discrete-event simulations composed of persistent entities, legal behavioral topologies, stochastic transition semantics, scenarios, causal events, logical time, and pluggable persistence.

The fundamental separation is:

```text
StateChart
    = legal behavioral topology

Probabilistic Transition Graph
    = stochastic semantics over that topology

Scenario Engine
    = external conditions and interventions

Scheduler
    = temporal semantics

SOSE Runtime
    = deterministic execution of all of the above
```

## Documents

| Document | Responsibility |
|---|---|
| `system-overview.md` | Overall architecture and component boundaries. |
| `domain-model.md` | Entities, relationships, master data, state, and events. |
| `statecharts.md` | Legal entity lifecycle behavior. |
| `probabilistic-transition-graph.md` | Stochastic semantics over legal transitions. |
| `scenario-engine.md` | External conditions, shocks, and interventions. |
| `scheduler.md` | Logical time and future work. |
| `runtime.md` | Canonical execution lifecycle. |
| `factories.md` | Canonical object construction. |
| `persistence.md` | State, event, schedule, and commit durability. |

## Architectural invariants

SOSE follows these core invariants:

1. Entity lifecycle state changes only through a StateChart.
2. StateCharts define legality, not stochastic choice.
3. Probabilities are evaluated only over currently enabled transitions.
4. Scenario logic does not directly perform ordinary lifecycle transitions.
5. Logical time is controlled exclusively by SOSE.
6. All stochastic decisions use deterministic scoped randomness.
7. Relevant synthetic identities are deterministic.
8. State and immutable events are persisted separately.
9. Cross-entity behavior is expressed through commands and events.
10. A simulation tick is committed only after all of its effects are durable.
11. Simulator-private metadata is not part of the external source-system contract.
12. The same initial state, seed, configuration, and inputs must produce the same simulation.

---

# `docs/architecture/system-overview.md`

# System Overview

SOSE is not a row generator.

It is an engine for modeling and executing synthetic operational systems.

A domain is represented as:

```text
Actors / Master Data
        ↓
Persistent Entities
        ↓
StateCharts
        ↓
Probabilistic Transition Graph
        ↓
Commands and Domain Events
        ↓
Cross-entity Causality
        ↓
Persistence
        ↓
Source Interfaces
```

## High-level architecture

```text
                   Domain Pack
                       │
       ┌───────────────┼────────────────┐
       │               │                │
    Entities       StateCharts       Scenarios
       │               │                │
       │               ↓                │
       │    Probabilistic Graph         │
       │               │                │
       └───────────────┼────────────────┘
                       ↓
                  SOSE Runtime
                       │
         ┌─────────────┼──────────────┐
         │             │              │
      Clock        Scheduler       Resources
         │             │              │
         └─────────────┼──────────────┘
                       ↓
                Commands / Events
                       │
              ┌────────┴────────┐
              │                 │
          Persistence      Source Interface
              │                 │
              ↓                 ↓
         State/Event Log    SQL / CDC / Kafka
```

## Component ownership

### Domain Pack

Defines domain-specific concepts:

- entities;
- relationships;
- StateCharts;
- transition weight policies;
- scenarios;
- resources;
- domain event payloads.

Examples:

```text
MRO
E-commerce
Banking
Logistics
Manufacturing
Healthcare
```

### SOSE Kernel

Contains domain-independent execution infrastructure:

```text
clock
scheduler
deterministic RNG
identity generation
factories
runtime
persistence contracts
```

### Source Interface

Represents what an external consumer would observe.

It must not expose simulator internals by default.

Example:

```text
internal:

simulation_tick
decision_scope
replay_sequence

external:

order_id
status
created_at
updated_at
customer_id
```

---

# `docs/architecture/domain-model.md`

# Domain Model

The domain model defines the persistent objects and relationships that constitute a synthetic operational system.

## Entity categories

SOSE distinguishes several conceptual entity types.

### Master data

Long-lived reference entities.

Examples:

```text
Supplier
Material
Warehouse
Customer
Account
Asset
Machine
Hospital
```

Master data normally exists before transactional behavior begins.

### Stateful operational entities

Entities whose lifecycle is controlled through a StateChart.

Examples:

```text
WorkOrder
PurchaseOrder
Claim
Shipment
Invoice
LoanApplication
Incident
```

### Event-like entities

Immutable business facts that may also exist as first-class source records.

Examples:

```text
Payment
InventoryMovement
GoodsReceipt
Transaction
SensorReading
```

## Base entity

A SOSE entity should have a stable identity and domain attributes.

Conceptually:

```python
Entity(
    id=...,
    entity_type=...,
    state=...,
    attributes=...,
)
```

Infrastructure metadata should remain separate when possible.

## Relationships

Relationships express domain structure.

Examples:

```text
WorkOrder → Asset
PurchaseOrder → Supplier
Order → Customer
Shipment → Order
Transaction → Account
```

Relationship cardinalities may include:

```text
one-to-one
one-to-many
many-to-one
many-to-many
```

## State versus event

SOSE explicitly distinguishes mutable state from immutable history.

State answers:

> What is true now?

Events answer:

> What happened?

Example:

```text
purchase_order_state
    status = RECEIVED
```

versus:

```text
PurchaseOrderSent
GoodsReceiptCreated
PurchaseOrderReceived
```

## Commands

Commands express intent.

Examples:

```text
ReleaseWorkOrder
ApproveRequisition
CapturePayment
DispatchShipment
```

Commands are not facts.

They may:

- succeed;
- be rejected;
- become invalid;
- trigger one or more state transitions.

## Domain events

Domain events represent observed facts.

Examples:

```text
WorkOrderReleased
MaterialShortageDetected
PurchaseRequisitionCreated
PaymentCaptured
ShipmentDispatched
```

Every event should preserve causal metadata where applicable:

```text
event_id
causation_id
correlation_id
occurred_at
entity_type
entity_id
```

---

# `docs/architecture/statecharts.md`

# StateCharts

StateCharts define the **legal behavioral topology** of stateful entities.

SOSE uses `python-statemachine` as its StateChart implementation.

The StateChart answers:

> Given this entity configuration and this event, is the transition legal?

It does not answer:

> Which enabled transition should occur probabilistically?

That belongs to the Probabilistic Transition Graph.

## Example

```text
PLANNED
   ↓ release
RELEASED
   ├── start ─────────→ IN_PROGRESS
   ├── wait ──────────→ WAITING_MATERIAL
   └── cancel ────────→ CANCELLED
```

The chart defines all three transitions as possible.

It does not determine their probabilities.

## Guards

Guards remain owned by the StateChart.

Example:

```text
RELEASED
   |
   └── start
         guard: material_available
```

A stochastic transition may only be considered after the StateChart determines that its event is enabled.

Runtime ordering:

```text
current configuration
        ↓
evaluate StateChart guards
        ↓
enabled events
        ↓
probabilistic evaluation
```

## Compound states

SOSE must support hierarchical state behavior.

Example:

```text
PROCESSING
├── VALIDATING
├── EXECUTING
└── REVIEWING
```

## Parallel regions

Example:

```text
ORDER
├── Fulfillment
│   ├── Picking
│   ├── Packing
│   └── Shipping
│
└── Payment
    ├── Authorized
    ├── Captured
    └── Refunded
```

This means runtime state must be represented as a configuration:

```text
StateConfiguration(
    fulfillment=Shipping,
    payment=Captured,
)
```

rather than one flat state string.

## Eventless transitions

Automatic/eventless transitions are owned by StateChart execution semantics.

They must not be sampled as independent stochastic decisions by SOSE.

---

# `docs/architecture/probabilistic-transition-graph.md`

# Probabilistic Transition Graph

The `ProbabilisticTransitionGraph` adds stochastic semantics to legal StateChart behavior.

The StateChart determines:

```text
what may happen
```

The probabilistic graph determines:

```text
how likely each enabled choice is
```

## Transition weights

SOSE models branch values as weights rather than requiring normalized probabilities.

Example:

```text
start               75
wait_for_material   20
cancel               5
```

Normalization produces:

```text
start               0.75
wait_for_material   0.20
cancel               0.05
```

## Guard-aware normalization

Suppose `start` becomes disabled.

The remaining distribution becomes:

```text
wait_for_material   20
cancel               5
```

Normalized:

```text
wait_for_material   0.80
cancel               0.20
```

Probability mass is never assigned to illegal transitions.

## Formal model

For state configuration `s`, let the enabled transition set be:

```text
E(s) = {e1, e2, ..., en}
```

Each transition has weight:

```text
w(e | context) >= 0
```

Then:

```text
P(e | s, context)
=
w(e | context)
/
Σ w(ej | context)
```

## Contextual weights

Weights may depend on simulation context.

Example:

```python
"start": lambda evaluation:
    8.0 if evaluation.entity.attributes["material_ready"] else 0.1
```

Context may include:

```text
entity attributes
related entities
resource availability
calendar
scenario effects
current logical time
capacity
external conditions
```

## Deterministic sampling

Sampling always uses SOSE deterministic randomness.

Conceptually:

```text
root seed
+ simulation tick
+ entity
+ state configuration
+ decision scope
```

produces the stochastic draw.

Therefore identical simulation conditions produce identical decisions.

## Decision record

SOSE should preserve sufficient information to explain every stochastic decision:

```text
active configuration
enabled events
raw weights
normalized probabilities
random draw
selected event
decision scope
```

## Event grouping

Probability is associated with semantic decisions/events, not blindly with physical graph edges.

This is important for parallel StateCharts where one event may trigger multiple transitions.

## Graph analysis

Future analysis may include:

```text
reachable states
absorbing states
expected steps
terminal-state probabilities
dominant paths
cycle probability
```

Static graphs resemble Markov chains.

Context-dependent graphs more closely represent:

```text
P(S[t+1] | S[t], X[t])
```

Commands may additionally yield MDP-like structures:

```text
P(S[t+1] | S[t], A[t])
```

---

# `docs/architecture/scenario-engine.md`

# Scenario Engine

The Scenario Engine represents **external conditions and interventions**.

It must not duplicate ordinary lifecycle logic already represented by the StateChart and Probabilistic Transition Graph.

## Responsibility

A scenario answers:

> What changed in the environment?

Examples:

```text
supplier disruption
machine failure
warehouse outage
fraud campaign
holiday demand spike
hospital surge
network outage
interest-rate change
```

## Scenario effects

A scenario may influence:

```text
entity attributes
resource availability
resource capacity
transition weights
delay distributions
costs
failure rates
service levels
calendars
```

Example:

```text
SupplierDisruption
        ↓
supplier.available = false
        ↓
receipt delay increases
        ↓
waiting-material transition weight increases
        ↓
maintenance completion time increases
```

The Scenario Engine changes context.

The Probabilistic Transition Graph reacts to that context.

## Scenario structure

Conceptually:

```python
Scenario(
    trigger=...,
    condition=...,
    effects=[...],
    duration=...,
    scope=...,
)
```

## Triggers

Potential trigger types:

```text
at_time
after_duration
on_tick
periodic
on_event
on_state_entry
on_state_exit
conditional
```

## Activation probability

Scenario stochasticity is separate from lifecycle transition stochasticity.

Example:

```yaml
scenario:
  supplier_disruption:
    activation_probability: 0.02
```

This means:

> Does the external disruption occur?

It does not mean:

> Which legal PurchaseOrder transition should occur?

## Effects

Scenario effects may be:

```text
instantaneous
temporary
persistent
scheduled-recovery
```

## Composition

Multiple scenarios may coexist.

SOSE must eventually define deterministic composition semantics for:

```text
priority
additive effects
multiplicative effects
replacement effects
conflicts
nested effects
```

---

# `docs/architecture/scheduler.md`

# Scheduler

The Scheduler defines the **temporal semantics** of SOSE.

It determines when commands, scenario effects, and future work become eligible for execution.

## Logical time

SOSE never requires real elapsed wall-clock time for simulation semantics.

Example:

```text
08:00  release work order
10:00  inspect asset
14:00  create requisition
next day 08:00 approve requisition
```

A simulation may execute months of logical time in seconds.

## Scheduled item

Conceptually:

```python
ScheduledItem(
    id=...,
    due_at=...,
    payload=...,
    priority=...,
)
```

Scheduled payloads may include:

```text
Command
Scenario activation
Scenario expiration
Resource release
Periodic callback
```

## Ordering

The scheduler must eventually define a total deterministic ordering:

```text
due_at
priority
sequence
deterministic id
```

This is necessary when multiple items occur at the same logical instant.

## Required operations

```text
schedule
cancel
reschedule
pop_due
peek_next
schedule_after
schedule_at
schedule_periodic
```

## Delay semantics

Future versions should support:

```text
constant
uniform
normal
log-normal
exponential
gamma
empirical
callable
```

Example:

```text
shipment delay ~ LogNormal(...)
```

## Business calendars

Future Scheduler integration should support:

```text
working hours
weekends
holidays
plant shutdowns
banking days
shift calendars
```

Scheduler time and SLA time may differ.

---

# `docs/architecture/runtime.md`

# SOSE Runtime

The runtime orchestrates deterministic execution of domain behavior.

It does not own domain-specific business rules.

## Canonical lifecycle

```text
load committed state
        ↓
restore scheduled work
        ↓
advance logical time
        ↓
activate due scenarios
        ↓
activate due commands
        ↓
evaluate StateChart legality
        ↓
evaluate probabilistic weights
        ↓
sample deterministic choice
        ↓
execute transition
        ↓
emit domain events
        ↓
schedule causal follow-up work
        ↓
persist changes
        ↓
commit simulation progress
```

## Execution boundary

A simulation step must not be considered committed until:

```text
entity state
events
scheduled work
scenario state
resource state
```

have all been made durable.

## Commands

Commands enter the execution pipeline.

```text
Command
   ↓
StateChart
   ↓
legal transition
   ↓
DomainEvent
```

## Causal reactions

A domain event may cause additional commands:

```text
MaterialShortageDetected
        ↓
CreatePurchaseRequisition
        ↓
PurchaseRequisitionCreated
```

This preserves explicit causal chains.

## Deterministic replay

Given:

```text
same initial persisted state
same root seed
same domain configuration
same scheduled work
same external inputs
```

SOSE must produce:

```text
same decisions
same entity identities
same state transitions
same event sequence
```

## Failure semantics

Future runtime work must define:

```text
retry
rollback
replay
partial persistence recovery
dead-letter behavior
```

---

# `docs/architecture/factories.md`

# Factories

Factories centralize construction of canonical SOSE objects.

Domain code should express intent rather than infrastructure metadata.

## EntityFactory

Responsibilities:

```text
deterministic identity
creation metadata
default values
initial lifecycle state
```

Example:

```python
work_order = ctx.entities.create(
    WorkOrder,
    key=(asset.id, occurrence),
    asset_id=asset.id,
)
```

## CommandFactory

Responsibilities:

```text
command identity
logical timestamp
target identity
causation
correlation
```

Example:

```python
command = ctx.commands.create(
    "purchase_requisition.approve",
    target=requisition,
    caused_by=created_event,
)
```

## EventFactory

Responsibilities:

```text
event identity
occurred_at
entity identity
causation_id
correlation_id
canonical metadata
payload
```

Example:

```python
event = ctx.events.create(
    "material.shortage_detected",
    entity=work_order,
    caused_by=command,
    material_id=material.id,
)
```

## Transition events

State changes should use standardized transition events.

Example:

```text
entity.state_transition

entity_type
entity_id
trigger
source_state
target_state
occurred_at
causation_id
correlation_id
```

This makes SOSE process-mining-ready by default.

## ScheduleFactory

Provides higher-level scheduling APIs:

```python
ctx.schedules.after(...)
ctx.schedules.at(...)
ctx.schedules.periodic(...)
```

Future APIs may include:

```python
ctx.schedules.business_delay(...)
```

## StateChartFactory

The StateChart factory manages lifecycle and binding rather than defining domain behavior.

Responsibilities:

```text
instantiate chart
bind entity
restore configuration
attach transition recorder
connect EventFactory
persist resulting configuration
```

The chart definition remains domain-owned.

---

# `docs/architecture/persistence.md`

# Persistence

Persistence allows SOSE simulations to survive process termination and support replay.

The engine must not depend directly on SQLite, PostgreSQL, Delta Lake, or any other specific storage technology.

## Persistence categories

SOSE persists conceptually separate data classes:

```text
Entity State
Domain Events
Scheduled Work
Scenario State
Resource State
Simulation Metadata
```

## State storage

State represents current truth.

Example:

```text
WorkOrder
id = WO-123
state = WAITING_MATERIAL
```

## Event storage

Events are append-only historical facts.

Example:

```text
WorkOrderReleased
MaterialShortageDetected
PurchaseRequisitionCreated
```

State and events must not be conflated.

## Commit metadata

SOSE maintains private progress metadata such as:

```text
committed_tick
logical_time
event_sequence
```

This metadata belongs to the simulator.

External consumers should not depend on it.

## Transaction boundary

Conceptually:

```text
begin simulation transaction
        ↓
persist entity changes
persist emitted events
persist scheduled work
persist scenario/resource changes
        ↓
advance committed simulation position
        ↓
commit
```

Failure before the final commit must not produce a partially committed logical tick.

## Persistence protocol

Conceptually:

```python
class Persistence(Protocol):

    def load_entity(...): ...
    def save_entity(...): ...

    def append_event(...): ...

    def save_scheduled_item(...): ...
    def delete_scheduled_item(...): ...

    def load_simulation_state(...): ...
    def commit_simulation_state(...): ...

    def transaction(...): ...
```

## Adapters

Planned adapters:

```text
MemoryPersistence
SQLitePersistence
PostgresPersistence
DeltaPersistence
```

Potential output adapters:

```text
Parquet
Kafka
CDC
```

## Separation from source interface

Persistence is how SOSE stores its simulation.

The source interface is what a simulated external system exposes.

These concepts must remain independent.

Example:

```text
SOSE Persistence
    contains:
        replay metadata
        scheduler state
        simulation state

Operational Source
    exposes:
        order
        invoice
        shipment
        payment
```

This prevents simulation implementation details from leaking into downstream analytics.
