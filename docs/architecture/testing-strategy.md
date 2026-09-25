# Testing Strategy

## Purpose

SOSE must be testable at multiple levels:

```text
pure deterministic primitives
        ↓
StateChart behavior
        ↓
probabilistic semantics
        ↓
scenario effects
        ↓
runtime execution
        ↓
persistence/replay
        ↓
domain behavior
        ↓
statistical properties
```

A simulation framework cannot rely only on ordinary unit tests.

It also requires deterministic replay tests, probabilistic property tests, state-model validation, and domain-level invariants.

---

## Testing layers

## 1. Core deterministic unit tests

Test:

```text
clock
identity generation
random scopes
scheduler ordering
factories
event metadata
correlation propagation
```

Example:

```python
assert deterministic_id("order", 1, 2) == deterministic_id("order", 1, 2)
```

and:

```python
assert (
    ctx.random.for_scope("x").random()
    ==
    ctx2.random.for_scope("x").random()
)
```

---

## 2. StateChart tests

Test legal lifecycle behavior independently from stochastic execution.

Example:

```text
PLANNED → RELEASED
must be legal

PLANNED → COMPLETED
must be illegal
```

Also test:

```text
guards
compound states
parallel states
final states
history states
eventless transitions
```

The StateChart tests answer:

> Is the behavioral topology correct?

---

## 3. Probabilistic graph tests

Test stochastic semantics independently from the Scenario Engine.

### Weight normalization

```text
weights:
  A = 3
  B = 1

probabilities:
  A = 0.75
  B = 0.25
```

### Guard filtering

If A becomes illegal:

```text
A removed
B = 1.0
```

### Contextual weights

Test that entity/resource/scenario context changes evaluated weights as intended.

### Deterministic draw

The same seed and scope must select the same event.

---

## 4. Factory tests

Factories define important invariants.

Test:

```text
EntityFactory
CommandFactory
EventFactory
ScheduleFactory
StateChartFactory
```

Example expectations:

- IDs are stable;
- timestamps come from logical clock;
- causation propagates;
- correlation propagates;
- transition events use canonical payloads.

---

## 5. Scheduler tests

Test:

```text
due ordering
same-time ordering
cancellation
rescheduling
recurrence
logical-time advancement
```

No scheduler test should require sleeping in real time.

Avoid:

```python
time.sleep(...)
```

---

## 6. Scenario tests

A scenario test should validate both activation and effects.

Example:

```text
supplier disruption activates
        ↓
supplier capacity decreases
        ↓
receipt delay policy changes
        ↓
effect expires
        ↓
capacity restored
```

Test:

```text
trigger
condition
activation probability
duration
effect application
effect rollback
composition
priority
```

---

## 7. Resource and queue tests

Validate:

```text
capacity never negative
exclusive resources not double-allocated
queue ordering deterministic
release wakes next eligible work
atomic multi-resource acquisition
```

---

## 8. Persistence contract tests

Every persistence adapter should pass the same contract suite.

Adapters:

```text
MemoryPersistence
SQLitePersistence
PostgresPersistence
DeltaPersistence
```

Contract tests should validate:

```text
save/load entity
append event
transaction behavior
scheduler persistence
committed position
rollback semantics
restart
```

---

## 9. Replay tests

Replay tests are fundamental.

### Full replay

```python
a = simulate(seed=42)
b = simulate(seed=42)

assert a.snapshot == b.snapshot
assert a.events == b.events
```

### Restart replay

Run:

```text
ticks 1–50
persist
terminate process
restore
ticks 51–100
```

Compare with:

```text
ticks 1–100 uninterrupted
```

The observable result should match.

---

## 10. Domain invariant tests

Every domain pack should declare business invariants.

MRO examples:

```text
received quantity <= ordered quantity
payment requires payable item
closed work order cannot reopen unless explicitly modeled
stock cannot violate configured constraints
```

E-commerce examples:

```text
refund requires captured payment
delivered shipment must belong to an order
cancelled order cannot be shipped
```

These are stronger than generic engine tests.

---

## 11. Causal integrity tests

Validate:

```text
causation_id references existing object
correlation_id propagates correctly
effect logical time >= cause logical time
cross-entity chain remains connected
```

Optional graph validation:

```text
no accidental orphan causal nodes
no causal cycles unless intentionally modeled
```

---

## 12. Source-interface tests

The source interface should be tested independently from internal persistence.

Validate:

```text
private SOSE fields hidden
stable source identifiers
correct source schema
correct CDC sequence
source commits coherent
```

---

## 13. Process-mining tests

Validate canonical event-log properties:

```text
case ID present
activity present
timestamps ordered deterministically
transition states consistent
resource IDs valid
```

For object-centric exports, validate object/event relationships.

---

## 14. Statistical tests

Determinism does not guarantee the model has correct long-run behavior.

For simple static transition probabilities, run many independent seeds.

Example target:

```text
P(cancel) = 0.05
```

Across many replications:

```text
observed cancellation rate ≈ 0.05
```

Tests should use tolerance intervals rather than exact equality.

Statistical tests should be:

- reproducible;
- large enough to detect gross errors;
- tolerant enough to avoid flaky CI.

---

## 15. Property-based testing

Property-based testing is especially useful for SOSE.

Candidate properties:

```text
normalized probabilities sum to 1
weights are non-negative
capacity never negative
same seed produces same trace
final states have no illegal outgoing execution
causal references are valid
```

Libraries such as Hypothesis can generate edge cases across:

```text
seeds
weights
timestamps
entities
queue sizes
resource capacities
```

---

## 16. Metamorphic testing

Some simulations have no simple expected output.

Metamorphic properties help.

Example:

```text
increase supplier lead time
    should not reduce
average procurement cycle time
```

or:

```text
increase technician capacity
    should not increase
resource waiting solely because of capacity
```

These properties require careful domain assumptions but can catch subtle errors.

---

## 17. Golden trace tests

For a small domain and fixed seed, maintain a short expected event trace.

Example:

```text
tick 1  WorkOrderCreated
tick 2  WorkOrderReleased
tick 3  MaterialShortageDetected
tick 3  PurchaseRequisitionCreated
```

Golden traces are useful for detecting accidental semantic changes.

They should remain small to avoid making legitimate refactoring painful.

---

## 18. Architecture tests

Test architectural boundaries as executable contracts.

Examples:

```text
scenario modules do not mutate entity.state directly
domain modules do not import persistence implementation classes
source adapters do not access simulator-private fields directly
all stochastic policies use SOSE random provider
```

Some of these can be checked through AST/import analysis.

---

## 19. Performance tests

Benchmark:

```text
events/sec
entities/sec
scheduler throughput
persistence throughput
memory growth
checkpoint time
```

Performance tests should not replace correctness tests.

---

## Suggested test layout

```text
tests/
├── unit/
│   ├── core/
│   ├── factories/
│   ├── probability/
│   └── scheduler/
│
├── contracts/
│   ├── persistence/
│   └── source_interface/
│
├── integration/
│   ├── runtime/
│   ├── replay/
│   └── scenarios/
│
├── domains/
│   └── mro/
│
├── statistical/
│
└── architecture/
```

---

## Simulation test DSL

SOSE may eventually provide:

```python
simulation.given(...)
simulation.when(...)
simulation.advance(...)
simulation.expect(...)
```

Example:

```python
simulation.given(
    work_order(state="released"),
    material_available(False),
)

simulation.advance(hours=4)

simulation.expect(
    work_order_state("waiting_material")
)
```

This API should remain a testing façade over normal runtime semantics.

It must not introduce a second execution engine.

---

## CI tiers

Recommended CI strategy:

### Fast CI

Run on every change:

```text
unit
architecture
StateChart
factory
probability
scheduler
small integration
```

### Extended CI

Run periodically or before release:

```text
persistence contracts
restart/replay
statistical
large domain simulations
performance
```

---

## Invariants

1. Tests never depend on real-time sleeps for logical behavior.
2. Random tests always use explicit seeds.
3. Replay is tested as a first-class capability.
4. Persistence adapters share contract tests.
5. Domain packs define business invariants.
6. Statistical tests are reproducible and non-flaky.
7. Test helpers use the same runtime semantics as production simulations.
8. Architecture boundaries are executable where practical.

---

## Definition of done

A new SOSE feature is not complete until it has appropriate tests across the relevant layers.

For example, a new scenario effect may require:

```text
unit test
scenario integration test
replay test
domain invariant test
```

depending on its behavior.
