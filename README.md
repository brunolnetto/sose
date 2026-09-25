# SOSE — Synthetic Operational System Engine

SOSE simulates operational systems rather than generating independent rows.

Its core model is:

```text
current state
+ legal behavior
+ stochastic semantics
+ logical time
+ external scenarios
+ causal events
= next operational state
```

SOSE is intended for domains with persistent entities, explicit lifecycle rules,
causal relationships, exceptions and time: MRO, logistics, e-commerce, banking,
manufacturing, healthcare, telecom, insurance, construction and supply chain.

## v0.5 architecture

```text
StateChart
    = legal behavioral topology

Probabilistic Transition Graph
    = stochastic semantics over that topology

Scenario Engine
    = external conditions and interventions

Scheduler
    = temporal semantics

SOSE runtime
    = deterministic execution of all of the above
```

The key v0.5 change is introducing a **simulation-backend boundary**. SOSE continues to own domain semantics, determinism, causality and persistence contracts, while SimPy becomes the first optional backend for in-memory discrete-event timing and resource contention.

## v0.5 capabilities

- deterministic logical clock;
- scoped deterministic randomness;
- deterministic entity, command and event identities;
- priority scheduler over logical time;
- explicit commands and immutable domain events;
- canonical factories for entities, commands, events and schedules;
- `python-statemachine` `StateChart` integration behind a runtime factory;
- extraction of legal transition topology from StateCharts;
- probabilistic policies attached without forking `python-statemachine`;
- guard filtering before probability normalization;
- contextual transition weights;
- deterministic stochastic sampling;
- support for multi-state configurations needed by compound/parallel charts;
- eventless transitions preserved as topology but left to SCXML execution;
- causal metadata (`causation_id`, `correlation_id`) propagated by construction;
- standardized state-transition events suitable for process mining;
- state persisted independently from immutable event history;
- persistence protocol with an in-memory adapter;
- MRO vertical slice;
- deterministic Scenario Engine;
- tick, event and scheduled scenario triggers;
- scenario conditions and activation probabilities;
- temporary scenario activations;
- environment attribute overlays;
- transition-weight modifiers and composite effects;
- automatic scenario integration with probabilistic transition evaluation;
- backend-neutral temporal/resource contracts;
- optional SimPy discrete-event backend;
- inclusive `run_until()` boundary semantics;
- deterministic same-time priority ordering;
- backend-neutral resource requests, leases and snapshots;
- priority-based resource contention without exposing SimPy objects.

## Behavioral boundary

`python-statemachine` owns:

```text
states
transitions
guards
validators
compound / parallel semantics
history
SCXML microsteps
```

SOSE owns:

```text
probabilistic transition semantics
logical time
deterministic randomness
causal scheduling
persistence
cross-entity effects
future scenario interventions
```

A useful rule is:

> StateChart decides what is legal. SOSE decides how likely each currently legal
> event is and samples that decision deterministically.

## Public domain-facing API

A domain author primarily works through `SimulationContext`:

```python
ctx.clock
ctx.random
ctx.entities
ctx.commands
ctx.events
ctx.schedules
ctx.transitions
ctx.scenarios
ctx.statecharts

# v0.5 execution backends are constructed explicitly
# and are not yet owned by SimulationContext.
```

### Entity creation

```python
work_order = ctx.entities.create(
    WorkOrder,
    key=(asset_id, occurrence),
    state="planned",
    attributes={"priority": "HIGH"},
)
```

### Stochastic StateChart semantics

```python
from sose.probability import probabilistic_transitions


@probabilistic_transitions({
    "start": 0.75,
    "wait_for_material": 0.20,
    "cancel": 0.05,
})
class WorkOrderChart(StateChart):
    ...
```

Those values are weights. After StateChart guards are evaluated, SOSE normalizes
only the enabled branches.

### Contextual weights

```python
@probabilistic_transitions({
    "start": lambda ev: 8.0 if ev.entity.attributes["material_ready"] else 0.1,
    "wait_for_material": lambda ev: 0.1 if ev.entity.attributes["material_ready"] else 8.0,
    "cancel": 0.1,
})
class WorkOrderChart(StateChart):
    ...
```

### Direct decision API

Once statecharts are bound through the engine:

```python
decision = ctx.statecharts.decide(
    work_order,
    guard_kwargs={"material_available": True},
    scope=("normal-operation",),
)

print(decision.event)
print(decision.probability)
```

The decision itself does not mutate the chart. Sending/dispatching the selected event
remains an explicit operation.

## Guard-before-probability rule

Given:

```text
start               0.75
wait_for_material   0.20
cancel              0.05
```

if a StateChart guard disables `start`, the effective distribution becomes:

```text
wait_for_material   0.80
cancel              0.20
```

not `0.20 / 0.05` with a missing 75% probability mass.

## Causal flow

```text
external condition / scenario
        ↓
StateChart legal topology + guards
        ↓
Probabilistic Transition Graph
        ↓
deterministic event selection
        ↓
CommandFactory
        ↓
StateChart.send(event)
        ↓
EventFactory
        ↓
DomainEvent
        ↓
Persistence
```

`causation_id` links an event or command to its immediate predecessor.
`correlation_id` identifies the broader business flow across multiple entities.

## Package layout

```text
src/sose/
├── core/
│   ├── clock.py
│   ├── context.py
│   ├── engine.py
│   ├── events.py
│   ├── identity.py
│   ├── randomness.py
│   └── scheduler.py
├── probability/
│   ├── builder.py
│   ├── graph.py
│   ├── model.py
│   ├── policy.py
│   └── runtime.py
├── factories/
│   ├── entity.py
│   ├── command.py
│   ├── event.py
│   ├── schedule.py
│   └── statechart.py
├── statecharts/
│   ├── base.py
│   └── topology.py
├── domain/
│   ├── entity.py
│   └── registry.py
├── scenarios/
│   ├── engine.py
│   ├── model.py
│   └── rules.py
├── backends/
│   ├── base.py
│   └── simpy.py
├── persistence/
│   ├── base.py
│   └── memory.py
├── dsl/
│   ├── loader.py
│   └── models.py
└── examples/mro/
    ├── entities.py
    ├── statecharts.py
    └── simulation.py
```



## SimPy backend

Install the optional backend with:

```bash
pip install "sose[simpy]"
```

The public API remains backend-neutral:

```python
from datetime import datetime, timedelta, timezone

from sose.backends.simpy import SimPyBackend

backend = SimPyBackend(
    origin=datetime(2026, 1, 1, tzinfo=timezone.utc),
)

backend.schedule_after(
    timedelta(hours=2),
    callback,
    priority=100,
)

backend.create_resource("technicians", capacity=3)
```

Domain code does not receive `simpy.Environment`, `simpy.Event`, generators, or native resource-request objects.

v0.5 intentionally keeps this backend ephemeral. The existing SOSE scheduler remains valid until v0.6 defines durable scheduled-work and resource-reservation reconstruction.

## Kernel invariants

1. Domain scenarios do not mutate entity state directly.
2. Valid lifecycle transitions and guards are owned by StateCharts.
3. Probability is evaluated only across currently legal/enabled events.
4. Probabilistic policies use weights; normalization is a runtime concern.
5. Stochastic selection uses only scoped deterministic RNG.
6. Eventless StateChart transitions are never sampled by SOSE.
7. Scenario interventions will alter context/policies, not duplicate lifecycle topology.
8. State changes produce immutable domain events.
9. Domain logic never calls the wall clock directly.
10. Synthetic identities are deterministic.
11. State and event history are persisted separately.
12. A tick is committed only after its effects are durable.
13. Simulator-private bookkeeping is not part of the downstream source contract.

## Development direction

v0.5 establishes the simulation-backend boundary and provides SimPy as the first execution backend. The next milestone is durable discrete-event execution: persisted scheduled work and resource reservations must be sufficient to rebuild a fresh backend after restart without serializing SimPy environments or Python generator stacks.

See:

- [`docs/factories.md`](docs/factories.md)
- [`docs/probabilistic-transition-graph.md`](docs/probabilistic-transition-graph.md)
- [`docs/architecture/scenario-engine.md`](docs/architecture/scenario-engine.md)
- [`docs/architecture/simpy-backend.md`](docs/architecture/simpy-backend.md)
