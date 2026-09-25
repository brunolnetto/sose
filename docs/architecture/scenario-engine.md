# Scenario Engine

## Purpose

The Scenario Engine models **external conditions and interventions** that change the environment in which operational entities behave.

It does not own lifecycle topology and it does not select StateChart transitions directly.

```text
StateChart
    = what is legally possible

Probabilistic Transition Graph
    = how likely each enabled choice is

Scenario Engine
    = what external conditions change those probabilities and contextual values
```

## v0.4 contract

SOSE v0.4 introduces a deterministic in-memory Scenario Engine with:

- tick triggers;
- domain-event triggers;
- scheduled triggers evaluated against logical time;
- predicates/conditions;
- deterministic activation probability;
- idempotent activation attempts;
- temporary activations with expiration;
- environment attribute overlays;
- transition-weight multipliers;
- composite effects;
- event causation/correlation propagation;
- automatic integration with probabilistic transition evaluation.

The Scenario Engine remains independent from the StateChart implementation.

## Basic model

```python
from datetime import timedelta

from sose.scenarios import (
    AttributeEffect,
    Scenario,
    TickTrigger,
    TransitionWeightEffect,
)

scenario = Scenario(
    name="supplier_disruption",
    trigger=TickTrigger(),
    activation_probability=0.05,
    duration=timedelta(hours=12),
    effects=(
        AttributeEffect("supplier.availability", "degraded"),
        TransitionWeightEffect(
            "wait_for_material",
            entity_type="work_order",
            multiplier=4.0,
        ),
    ),
)

ctx.scenarios.register(scenario)
```

The effect changes simulation context. It does not execute `wait_for_material`.

## Triggers

### `TickTrigger`

Evaluates on logical simulation ticks.

```python
TickTrigger(every=4, offset=1)
```

This matches ticks `1, 5, 9, 13, ...`.

### `EventTrigger`

Subscribes to immutable SOSE domain events.

```python
EventTrigger(
    event="order.paid",
    entity_type="order",
)
```

An event-triggered activation inherits `causation_id` and `correlation_id` from the source event.

### `ScheduledTrigger`

Represents a one-shot intervention that becomes eligible when logical time reaches its configured timestamp.

```python
ScheduledTrigger(at=planned_outage_at)
```

In v0.4 this is evaluated during tick processing. Exact discrete-event scheduling belongs to the future temporal backend abstraction.

## Conditions

A condition receives a `ScenarioEvaluation` containing the scenario, signal, and `SimulationContext`.

Conditions run before the activation-probability draw. A failed condition records `reason = condition_false` and consumes no random draw.

## Deterministic activation

Each trigger occurrence derives a deterministic attempt identity from the scenario name and trigger key.

The activation draw uses a scoped deterministic stream:

```text
root seed
    +
scenario-activation
    +
scenario name
    +
trigger occurrence
```

Re-evaluating the same occurrence returns the same decision instead of reapplying the effect.

## Re-entry

Scenarios are non-reentrant by default. While a scenario is active, later matching triggers record `reason = already_active` rather than stacking its effects.

Domains may explicitly opt into overlapping activations with `allow_reentry=True`.

## Effects

### Environment attributes

`AttributeEffect` writes to a Scenario Engine overlay rather than mutating `Entity.attributes`.

```python
AttributeEffect("plant.available", False)
```

This makes temporary effects reversible without reconstructing persisted entities.

### Transition-weight effects

```python
TransitionWeightEffect(
    "wait_for_material",
    entity_type="work_order",
    multiplier=4.0,
)
```

Modifiers compose multiplicatively across active scenarios.

If the base weights are:

```text
start = 1
wait  = 1
```

and `wait` receives a multiplier of `4`, the effective distribution becomes:

```text
start = 0.20
wait  = 0.80
```

A zero multiplier disables a branch probabilistically while preserving StateChart topology.

### Composite effects

```python
CompositeEffect(
    (
        AttributeEffect("inventory.shortage", True),
        TransitionWeightEffect("wait", multiplier=4.0),
    )
)
```

## Temporary activations

A scenario may define a positive `duration`. Active effects stop contributing after `expires_at`.

The in-memory runtime expires effects during tick/event evaluation and through explicit `expire_due()`.

## Integration with probabilistic transitions

The ordering is:

```text
StateChart configuration
        ↓
guards / enabled events
        ↓
domain base weights
        ↓
active scenario weight modifiers
        ↓
weight validation
        ↓
normalization
        ↓
deterministic sampling
```

The Scenario Engine never calls `StateChart.send()`.

## Explicit limitations of v0.4

v0.4 deliberately does **not** implement:

- durable scenario-state persistence;
- exact discrete-event scheduling of scenario activations;
- resource/capacity effects;
- calendar effects;
- delay-distribution effects;
- source-system effects;
- DSL compilation into runtime `Scenario` objects;
- SimPy integration.

Those belong to subsequent milestones.

```text
v0.4 Scenario Engine
        ↓
v0.5 simulation backend abstraction / SimPy backend
        ↓
v0.6 durable discrete-event runtime
```

## Invariants

1. Scenarios do not mutate lifecycle state directly.
2. Scenarios do not select ordinary StateChart branches.
3. Activation randomness is scoped and deterministic.
4. The same trigger occurrence is idempotent.
5. Non-reentrant scenarios do not stack while active.
6. Temporary effects disappear after expiration.
7. Transition modifiers operate after legality filtering and before normalization.
8. Scenario attributes are overlays, not hidden entity mutations.
9. Event-triggered activations preserve causal metadata.
10. Scenario state remains simulator-private unless explicitly projected.
