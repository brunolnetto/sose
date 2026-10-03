# SOSE Roadmap

## Synthetic Operational System Engine

SOSE is a framework for simulating operational systems with:

- persistent entities;
- legal state transitions;
- probabilistic behavior;
- causal events;
- deterministic randomness;
- logical time;
- scenarios and interventions;
- persistence;
- analytical/source interfaces.

The core architectural separation is:

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

---

# Current status

The current implementation already includes the first SOSE kernel.

## Implemented

### Core runtime

- logical simulation clock;
- deterministic random-number generation;
- deterministic identities;
- scheduler;
- commands;
- domain events;
- simulation context;
- simulation engine.

### Domain model

- persistent entities;
- entity registry;
- entity factories;
- command factories;
- event factories;
- schedule factories.

### State machines

- integration boundary with `python-statemachine`;
- support for `StateChart`;
- transition recording;
- statechart factory;
- statechart topology introspection.

### Probabilistic behavior

- `ProbabilisticTransitionGraph`;
- transition edges;
- state configurations;
- static transition weights;
- contextual transition weights;
- normalization of enabled transitions;
- deterministic sampling;
- guard filtering before normalization;
- grouping by event;
- support groundwork for hierarchical and parallel statecharts.

### Persistence

- persistence protocol;
- in-memory implementation;
- state persistence;
- event persistence;
- committed simulation tick.

### DSL groundwork

- initial domain specification models;
- loader structure;
- separation between:
  - transition stochasticity;
  - scenario activation probability.

### Example domain

- initial MRO domain;
- `WorkOrder` example;
- statechart;
- probabilistic branch selection.

---

# Phase 1 — Stabilize the kernel

Goal:

> Make the current execution model explicit, testable, and stable before adding more domain-level capabilities.

## 1.1 Formal execution lifecycle

Define the canonical SOSE execution cycle:

```text
load committed state
        ↓
advance logical clock
        ↓
activate due scheduled work
        ↓
evaluate scenarios
        ↓
produce commands
        ↓
evaluate legal transitions
        ↓
evaluate probabilistic weights
        ↓
sample deterministically
        ↓
execute state transitions
        ↓
emit domain events
        ↓
apply cross-entity reactions
        ↓
persist state + events
        ↓
commit tick
```

Tasks:

- formalize `SimulationTick`;
- define tick transaction boundaries;
- define failure/retry semantics;
- guarantee idempotent replay;
- define event ordering within a tick;
- define deterministic sequencing for simultaneous events.

## 1.2 Runtime invariants

Encode architectural invariants as tests.

Examples:

- entities cannot mutate lifecycle state outside a StateChart;
- scenarios cannot directly mutate entities;
- all stochastic behavior uses SOSE RNG;
- all important IDs are deterministic;
- state and events are persisted separately;
- committed tick advances only after persistence succeeds;
- event causation chains remain valid;
- probability normalization only includes enabled transitions.

## 1.3 Error model

Introduce explicit SOSE exceptions:

```text
InvalidTransitionError
InvalidProbabilityModelError
ZeroProbabilityMassError
ReplayMismatchError
PersistenceConflictError
ScenarioExecutionError
SchedulingError
DomainInvariantError
```

---

# Phase 2 — Scenario Engine

Goal:

> Model external conditions, interventions, shocks, policies, and cross-entity influences without embedding them into statecharts.

The Scenario Engine should not decide ordinary lifecycle branches.

Its role is to change the environment in which those decisions occur.

## 2.1 Scenario abstraction

Introduce:

```python
Scenario
ScenarioTrigger
ScenarioCondition
ScenarioEffect
ScenarioDuration
ScenarioScope
```

Possible triggers:

```text
on_tick
on_event
on_state_entry
on_state_exit
at_time
after_duration
periodic
conditional
```

## 2.2 Scenario effects

Scenarios may modify:

- entity attributes;
- shared resources;
- capacities;
- calendars;
- transition weights;
- delay distributions;
- availability;
- costs;
- failure rates;
- service levels.

Example:

```text
Supplier disruption
    ↓
supplier availability decreases
    ↓
PO receipt delay increases
    ↓
probability of material shortage increases
    ↓
maintenance waiting time increases
```

## 2.3 Temporary and permanent effects

Support:

```text
instantaneous effect
temporary effect
persistent effect
scheduled recovery
```

Example:

```text
warehouse outage
duration = 8 hours
capacity multiplier = 0
```

## 2.4 Scenario composition

Support multiple simultaneous scenarios.

Questions to solve:

- effect precedence;
- additive vs multiplicative effects;
- conflicting interventions;
- nested scenarios;
- scenario priority;
- deterministic resolution order.

---

# Phase 3 — Temporal semantics

Goal:

> Move from a simple logical clock to a proper discrete-event simulation time model.

## 3.1 Delay distributions

Support:

```text
constant
uniform
normal
log-normal
exponential
gamma
empirical distribution
custom callable
```

Example:

```python
receipt_delay = LogNormal(...)
```

## 3.2 Business calendars

Introduce:

```text
Calendar
BusinessDayCalendar
ShiftCalendar
HolidayCalendar
MaintenanceWindow
```

Examples:

- warehouse operates 08:00–18:00;
- weekends excluded;
- plant shutdown periods;
- payment processing only on business days.

## 3.3 SLA clocks

Support clocks that differ from wall-clock elapsed time.

Examples:

```text
business hours
working shifts
banking days
maintenance hours
```

## 3.4 Event queue semantics

Formalize:

- priority queue ordering;
- same-time event ordering;
- cancellation;
- rescheduling;
- recurring events;
- delayed commands;
- delayed scenario effects.

---

# Phase 4 — Resources, capacity, and queues

Goal:

> Allow entities to compete for constrained operational resources.

This is required for realistic:

- logistics;
- manufacturing;
- hospitals;
- maintenance;
- airports;
- call centers;
- warehouses.

## 4.1 Resource model

Introduce:

```python
Resource
ResourcePool
Capacity
Allocation
Reservation
Release
```

Examples:

```text
technicians
hospital beds
warehouse docks
machines
vehicles
inspection teams
loading bays
```

## 4.2 Queue model

Support:

```text
FIFO
LIFO
priority queue
deadline-aware
weighted priority
custom policy
```

## 4.3 Blocking behavior

State transitions may depend on resource acquisition.

Example:

```text
WORK_ORDER.RELEASED
        ↓
request technician
        ↓
available?
    yes / no
     ↓     ↓
IN_PROGRESS
        WAITING_RESOURCE
```

---

# Phase 5 — Advanced StateCharts

Goal:

> Fully exploit hierarchical state-machine semantics without flattening them prematurely.

## 5.1 Compound states

Example:

```text
PROCESSING
├── VALIDATING
├── EXECUTING
└── REVIEWING
```

## 5.2 Parallel regions

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

The SOSE state representation must therefore remain based on:

```text
StateConfiguration
```

rather than one flat state.

## 5.3 History states

Support restoring previous nested state configurations.

## 5.4 Reachability analysis

Introduce graph analysis:

- reachable states;
- unreachable states;
- terminal states;
- absorbing states;
- cycles;
- dead ends;
- impossible transitions.

---

# Phase 6 — Probabilistic model analysis

Goal:

> Make SOSE useful not only for execution, but also for analyzing the modeled system.

## 6.1 Static graph analysis

For static weights:

- transition matrix;
- absorption probabilities;
- expected transitions to terminal state;
- most likely paths;
- cycle probability.

## 6.2 Contextual graph analysis

For contextual probabilities:

```text
P(S[t+1] | S[t], X[t])
```

Support evaluation under a provided context.

## 6.3 Monte Carlo analysis

Run repeated deterministic-seeded simulations to estimate:

- completion probability;
- SLA breach probability;
- expected duration;
- expected cost;
- tail risk;
- throughput;
- failure probability.

## 6.4 Explainability

For every stochastic decision persist:

```text
active configuration
enabled events
evaluated weights
normalized probabilities
random draw
selected event
decision scope
```

This enables replay and debugging.

---

# Phase 7 — Persistence adapters

Goal:

> Separate simulation semantics from storage technology.

## 7.1 SQLite

First durable local backend.

Use cases:

- tests;
- notebooks;
- demos;
- small simulations.

## 7.2 PostgreSQL

Operational simulation backend.

Support:

- transactions;
- relational constraints;
- concurrent consumers;
- CDC tools.

## 7.3 Delta Lake

Data-engineering-oriented backend.

Support:

- state tables;
- append-only event tables;
- Change Data Feed;
- Databricks integration.

## 7.4 Parquet

Portable batch output.

## 7.5 Kafka / event streaming

Optional event sink:

```text
SOSE
  ↓
DomainEvent
  ↓
Kafka
```

State persistence should remain independent.

---

# Phase 8 — Source-system interface

Goal:

> Make simulated systems observable exactly like real operational systems.

Possible interfaces:

```text
SQL tables
CDC
event streams
REST APIs
files
message queues
```

A crucial invariant:

```text
SOSE internal implementation
        ≠
external source-system contract
```

Internal fields such as:

```text
simulation_tick
internal sequence
replay metadata
scenario internals
```

must not automatically appear in the external source representation.

---

# Phase 9 — Domain DSL

Goal:

> Allow new synthetic operational systems to be declared without rewriting the engine.

The DSL should describe behavior, not replace Python.

Example:

```yaml
domain: ecommerce

entities:

  order:
    statechart: OrderChart

    relationships:
      customer:
        target: customer
        cardinality: many-to-one

probabilistic_transitions:

  order:

    paid:
      ship: 0.97
      cancel: 0.03

scenarios:

  carrier_disruption:
    trigger:
      type: random
      activation_probability: 0.01

    effects:
      - target: shipment_delay
        multiplier: 3.0

    duration:
      hours: 12
```

## 9.1 DSL validation

Validate:

- referenced entities;
- referenced events;
- transition names;
- probability definitions;
- scenario targets;
- relationship integrity;
- distribution parameters.

## 9.2 DSL compilation

Compile:

```text
DomainSpec
    ↓
DomainRegistry
StateCharts
Transition policies
Scenario definitions
Resources
Calendars
```

---

# Phase 10 — Domain packs

Goal:

> Demonstrate that SOSE is truly domain-independent.

Recommended sequence:

## 10.1 MRO

Use as reference implementation.

Model:

```text
maintenance
inventory
procurement
receiving
fiscal
accounts payable
payments
```

## 10.2 E-commerce

```text
Customer
Cart
Order
Payment
Shipment
Delivery
Return
```

## 10.3 Logistics

```text
Order
Pickup
Hub
Transfer
Last-mile
Delivery
Return
```

## 10.4 Banking / Payments

```text
Customer
Account
Transaction
Authorization
Settlement
Reconciliation
Dispute
```

## 10.5 Manufacturing

```text
Production order
Material allocation
Machine allocation
Production
Inspection
Rework
Completion
```

Later candidates:

```text
Healthcare
Insurance
Telecom
Construction
SaaS
Supply Chain
```

---

# Phase 11 — Observability and process mining

Goal:

> Make every SOSE simulation explainable and process-mining-ready by default.

## 11.1 Canonical transition log

Every transition should produce a standardized event containing:

```text
entity_type
entity_id
event
source_state
target_state
occurred_at
tick
causation_id
correlation_id
scenario context
```

## 11.2 Simulation metrics

Track:

```text
entities created
events emitted
commands processed
transitions executed
queue size
scenario activations
resource utilization
simulation throughput
```

## 11.3 Process mining exports

Provide direct export to:

```text
case_id
activity
timestamp
event_id
resource
attributes
```

---

# Phase 12 — Testing framework

Goal:

> Make deterministic simulation testable as easily as ordinary application code.

Introduce helpers such as:

```python
simulation.given(...)
simulation.when(...)
simulation.advance(...)
simulation.expect(...)
```

Example:

```python
simulation.given(work_order(state="released"))
simulation.when(material_available=False)

simulation.advance(hours=4)

simulation.expect(
    WorkOrder,
    state="waiting_material",
)
```

Support:

- deterministic snapshots;
- replay assertions;
- event assertions;
- probability-distribution tests;
- statechart reachability tests;
- scenario tests.

---

# Phase 13 — Simulation experiments

Goal:

> Turn SOSE into an experimentation framework.

Introduce:

```python
Experiment
ParameterSweep
Replication
SimulationResult
```

Example:

```python
Experiment(
    parameter={
        "supplier_failure_rate": [0.01, 0.05, 0.10],
    },
    replications=100,
)
```

Outputs:

```text
throughput
cycle time
SLA
inventory
cost
failure probability
```

This enables operational what-if analysis.

---

# Phase 14 — Performance and scale

Goal:

> Support large simulations without changing domain semantics.

Potential improvements:

- batched persistence;
- lazy entity loading;
- checkpointing;
- event batching;
- vectorized generation where appropriate;
- partitioned event stores;
- multiprocessing for independent simulations;
- parallel Monte Carlo execution.

Important constraint:

> Parallel execution must never compromise deterministic replay of an individual simulation.

---

# Phase 15 — Public API stabilization

Goal:

> Reach a coherent `1.0` surface.

Proposed user-facing API:

```python
from sose import Simulation

simulation = Simulation(
    domain=domain,
    seed=42,
    persistence=storage,
)

simulation.bootstrap()

simulation.run(
    until="2027-01-01",
)
```

Domain-facing API:

```python
ctx.clock
ctx.random

ctx.entities
ctx.commands
ctx.events
ctx.schedules
ctx.statecharts

ctx.resources
ctx.scenarios
```

---

# Target architecture

```text
                   Domain Pack
                       │
        ┌──────────────┼──────────────┐
        │              │              │
    Entities       StateCharts     Scenarios
        │              │              │
        │              ↓              │
        │   Probabilistic Graph       │
        │              │              │
        └──────────────┼──────────────┘
                       ↓
                  SOSE Runtime
                       │
          ┌────────────┼────────────┐
          │            │            │
       Clock        Scheduler    Resources
          │            │            │
          └────────────┼────────────┘
                       ↓
                 Commands / Events
                       │
              ┌────────┴────────┐
              │                 │
          Persistence      Source Interface
              │                 │
              ↓                 ↓
        State + Event Log   SQL / CDC / Kafka
```

---

# Suggested implementation order

The recommended sequence from the current codebase is:

```text
[x] Kernel
[x] Factories
[x] StateChart integration
[x] Probabilistic Transition Graph

[ ] Scenario Engine
[ ] Temporal distributions and calendars
[ ] Resources and queues
[ ] Advanced StateCharts
[ ] Durable persistence
[ ] Domain DSL
[ ] Full MRO reference domain
[ ] Additional domain packs
[ ] Process-mining / observability layer
[ ] Experiment framework
[ ] Performance work
[ ] Public API stabilization
```

---

# Near-term milestones

## v0.4

**Scenario Engine**

Deliver:

- scenario abstraction;
- triggers;
- conditions;
- effects;
- transition-weight modifiers;
- temporary interventions;
- event subscriptions.

## v0.5

**Temporal and resource model**

Deliver:

- delay distributions;
- calendars;
- resource pools;
- capacity;
- queues;
- reservation/release semantics.

## v0.6

**Durable simulation**

Deliver:

- SQLite persistence;
- restart/replay;
- checkpoint recovery;
- durable scheduled events.

## v0.7

**Domain DSL**

Deliver:

- validated domain specs;
- declarative scenarios;
- declarative transition policies;
- domain loader.

## v0.8

**MRO reference implementation**

Deliver a complete multi-domain operational simulation:

```text
Maintenance
Inventory
Procurement
Receiving
Fiscal
AP
Payments
```

## v0.9

**Experiments and analysis**

Deliver:

- Monte Carlo runs;
- parameter sweeps;
- probabilistic graph analysis;
- process-mining exports;
- simulation metrics.

## v1.0

**Stable SOSE API**

Requirements:

- deterministic replay;
- restartability;
- multiple persistence adapters;
- advanced StateCharts;
- Scenario Engine;
- resource constraints;
- reusable domain packs;
- documented extension API;
- stable public contracts.

