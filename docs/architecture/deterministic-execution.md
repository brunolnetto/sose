# Deterministic Execution

## Purpose

Deterministic execution is a core SOSE contract.

SOSE is allowed to model stochastic systems, but the execution of that stochasticity must be reproducible.

Given the same:

- initial persisted state;
- root seed;
- domain configuration;
- StateChart definitions;
- transition policies;
- scenario definitions;
- scheduled work;
- external inputs;
- engine version;

SOSE must produce the same:

- synthetic identities;
- stochastic decisions;
- state transitions;
- domain events;
- causal chains;
- logical timestamps;
- committed simulation state.

The goal is not to eliminate randomness.

The goal is to make randomness **controlled, scoped, replayable, and explainable**.

---

## Architectural principle

```text
Stochastic model
        +
Deterministic execution
        =
Reproducible simulation
```

A simulation must never depend on process-global randomness such as:

```python
random.random()
```

or:

```python
uuid.uuid4()
```

for behavior that affects reproducibility.

Instead, stochastic decisions derive from explicit simulation context.

---

## Scoped randomness

Randomness is derived from a stable scope.

Conceptually:

```text
root seed
    +
simulation tick / logical time
    +
entity type
    +
entity id
    +
state configuration
    +
decision type
    +
decision scope
```

produces a deterministic random stream.

Example:

```python
rng = ctx.random.for_scope(
    "transition",
    work_order.entity_type,
    work_order.id,
    ctx.clock.tick,
    chart.configuration,
)
```

The same scope must produce the same pseudo-random sequence.

Different scopes must remain isolated.

This prevents unrelated changes in one part of the simulation from unnecessarily shifting random choices elsewhere.

---

## Deterministic identities

SOSE should use deterministic identity generation for relevant synthetic entities and events.

Example:

```python
purchase_order_id = ctx.identities.create(
    "purchase_order",
    supplier_id,
    requisition_id,
    occurrence,
)
```

instead of:

```python
purchase_order_id = uuid.uuid4()
```

Stable identities improve:

- retry safety;
- idempotency;
- replay;
- debugging;
- snapshot comparison;
- source-system consistency.

Not every implementation detail must use deterministic IDs, but anything observable or semantically relevant should.

---

## Deterministic ordering

Multiple pieces of work may become eligible at the same logical instant.

SOSE must define a total ordering.

Recommended ordering key:

```text
due_at
priority
creation_sequence
deterministic_id
```

The ordering must not depend on:

- dictionary iteration accidents;
- database query order without `ORDER BY`;
- thread scheduling;
- process scheduling;
- wall-clock timing.

---

## Tick determinism

A simulation tick is a logical execution boundary.

Conceptually:

```text
load committed state
        ↓
resolve due work
        ↓
evaluate scenarios
        ↓
evaluate commands
        ↓
evaluate transition legality
        ↓
evaluate stochastic weights
        ↓
sample decisions
        ↓
execute transitions
        ↓
emit events
        ↓
schedule causal follow-up work
        ↓
persist
        ↓
commit tick
```

If a failure occurs before commit, the tick should be replayable from the previous committed state.

---

## Decision records

Every stochastic decision should be explainable.

A transition decision should preserve enough information to reconstruct why the decision occurred.

Recommended decision metadata:

```text
decision_id
entity_type
entity_id
logical_time
state_configuration
enabled_events
raw_weights
normalized_probabilities
random_draw
selected_event
decision_scope
scenario_context
```

Decision metadata may remain simulator-private.

It does not automatically belong to the external source-system interface.

---

## Replay

Replay means executing the same logical simulation inputs again and obtaining the same observable result.

### Full replay

Start from the same initial state and rerun the entire simulation.

```text
initial state
     ↓
same seed
     ↓
same configuration
     ↓
same simulation history
```

### Checkpoint replay

Start from a committed checkpoint and reproduce only subsequent execution.

```text
checkpoint N
     ↓
restore state
     ↓
restore scheduler
     ↓
restore scenario/resource state
     ↓
continue deterministically
```

---

## Version sensitivity

Determinism is meaningful within a defined execution contract.

Changing any of the following may legitimately change output:

- StateChart topology;
- transition weights;
- scenario policies;
- random-scope construction;
- event ordering rules;
- persistence semantics;
- domain code;
- SOSE runtime behavior.

For strong reproducibility, a simulation run should record:

```text
SOSE version
domain version
configuration fingerprint
root seed
schema version
```

---

## Concurrency

Parallel execution must not alter the result of one simulation.

Safe parallelism includes:

```text
simulation A ─┐
              ├── independent workers
simulation B ─┘
```

A single simulation may eventually use internal parallelism, but only if ordering and random scopes remain deterministic.

Parallelism must not turn execution into:

```text
whoever finishes first wins
```

unless that behavior is explicitly part of the modeled system.

---

## Invariants

1. All stochastic behavior uses SOSE-managed randomness.
2. Observable synthetic identities are deterministic when retry/replay semantics require them.
3. Simultaneous work has a deterministic execution order.
4. A committed state is sufficient to resume execution.
5. Failed uncommitted work can be replayed safely.
6. Random scopes are stable and explicit.
7. Decision metadata is sufficient to explain stochastic choices.
8. External wall-clock timing never determines simulation behavior.
9. Independent simulation replications do not influence one another.

---

## Anti-patterns

Avoid using one global RNG across the entire engine. Unrelated implementation changes could shift later results.

Avoid:

```python
datetime.now()
```

for domain time. Use:

```python
ctx.clock.now
```

And avoid relying on unordered collections to resolve simultaneous events.

---

## Testing strategy

Deterministic execution should be validated with tests such as:

```python
result_a = run_simulation(seed=42)
result_b = run_simulation(seed=42)

assert result_a == result_b
```

and:

```python
result_a = run_simulation(seed=42)
result_b = run_simulation(seed=43)

assert result_a != result_b
```

Also test scoped stability where the architecture intends independence.

---

## Future work

- simulation fingerprints;
- deterministic distributed execution;
- replay audit tooling;
- decision-trace visualization;
- deterministic state snapshots;
- compatibility guarantees across SOSE releases.
