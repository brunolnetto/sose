# Probabilistic Transition Graph

SOSE v0.3 separates five concerns:

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

The probabilistic layer does **not** replace the StateChart. It projects the
StateChart's legal transition graph into a stochastic decision surface.

## Why weights, not stored probabilities

Suppose `released` has three legal events:

```text
start               weight 0.75
wait_for_material   weight 0.20
cancel              weight 0.05
```

If a StateChart guard disables `start`, SOSE does not keep stale probabilities.
It first asks the StateChart which events are enabled, then normalizes the
remaining weights:

```text
wait_for_material   0.20 / 0.25 = 0.80
cancel              0.05 / 0.25 = 0.20
```

Therefore the execution order is:

```text
active state configuration
        ↓
StateChart guards / enabled events
        ↓
legal enabled event set
        ↓
contextual transition weights
        ↓
normalization
        ↓
scoped deterministic RNG
        ↓
selected event
        ↓
StateChart.send(event)
```

## Attaching stochastic semantics to a StateChart

SOSE adds metadata around `python-statemachine`; it does not fork the library.

```python
from statemachine import State, StateChart
from sose.probability import probabilistic_transitions


@probabilistic_transitions({
    "start": 0.75,
    "wait_for_material": 0.20,
    "cancel": 0.05,
})
class WorkOrderChart(StateChart):
    released = State(initial=True)
    in_progress = State(final=True)
    waiting_material = State()
    cancelled = State(final=True)

    start = released.to(in_progress)
    wait_for_material = released.to(waiting_material)
    cancel = released.to(cancelled)
```

The class remains a normal `StateChart`. The decorator stores a SOSE
`TransitionPolicy` alongside it.

## StateChart topology extraction

`graph_from_statechart(chart)` traverses the StateChart states and their public
transition collections. Each legal transition becomes a `TransitionEdge` with:

- source state id;
- target state ids;
- triggering event;
- `internal` metadata;
- `initial` metadata.

Eventless transitions are included in the graph for analysis but are not sampled.
SCXML automatic microsteps remain owned by the StateChart runtime.

## Contextual weights

Weights can be functions of the entity and full simulation context:

```python
policy = probabilistic({
    "start": lambda ev: 8.0 if ev.entity.attributes["material_ready"] else 0.1,
    "wait": lambda ev: 0.1 if ev.entity.attributes["material_ready"] else 8.0,
    "cancel": 0.1,
})
```

The topology remains unchanged; only the stochastic semantics vary.

This supports future policies based on:

- inventory position;
- calendars and shifts;
- resource capacity;
- supplier reliability;
- SLA pressure;
- entity attributes;
- related-entity state;
- scenario interventions.

## Deterministic sampling

The runtime derives its PRNG from a scope containing at least:

```text
root seed
simulation tick
entity type
entity id
active state configuration
optional decision scope
```

The same simulation state and scope therefore yield the same stochastic decision,
which is essential for replay, debugging and idempotent retries.

## Hierarchical and parallel statecharts

`StateConfiguration` is represented as a set-like ordered tuple of active state ids,
not a single state string. This is deliberate: compound and parallel StateCharts can
have multiple active states at once.

The static graph stores legal edges by source state. A parent-state edge is therefore
available whenever that parent is part of the active hierarchical configuration.

For v0.3, selection is intentionally performed at **event** granularity. This avoids
double-counting one event when a parallel StateChart has multiple matching transition
edges. The StateChart itself remains responsible for resolving the exact SCXML
microsteps caused by the selected event.

## Boundary with the Scenario Engine

The Scenario Engine is intentionally not responsible for picking ordinary lifecycle
branches anymore.

It will later influence the probabilistic layer by changing context, weights or
external commands:

```text
Scenario: supplier disruption
        ↓
lead time / availability context changes
        ↓
wait_for_material weight increases
        ↓
Probabilistic Transition Graph
        ↓
normal deterministic SOSE execution
```

This prevents the future Scenario Engine from duplicating StateChart decision logic.
