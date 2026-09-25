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

## v0.3 architecture

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

The key v0.3 change is making `ProbabilisticTransitionGraph` a first-class kernel
component **before** building the full Scenario Engine.

## v0.3 goals

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
- MRO vertical slice.

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
ctx.statecharts
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
│   └── rules.py
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

v0.3 establishes the probabilistic behavioral layer. The next release can build the
Scenario Engine on top of it, focusing on external conditions, shocks, interventions,
resources and cross-entity causality rather than repeating branch-decision logic.

See:

- [`docs/factories.md`](docs/factories.md)
- [`docs/probabilistic-transition-graph.md`](docs/probabilistic-transition-graph.md)
