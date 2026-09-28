# Build your first durable SOSE domain

This guide builds the smallest useful SOSE model: one `TutorialJob` that moves

```text
queued -> running -> completed
```

The completion happens at a future logical time and must still happen after the
ephemeral backend is rebuilt.

The complete executable example lives in
`sose.examples.tutorial_job`.

## 1. Define durable domain state

```python
from dataclasses import dataclass

from sose.api import Entity


@dataclass(slots=True)
class TutorialJob(Entity):
    entity_type: str = "tutorial_job"
```

The entity is semantic truth. It is not a SimPy process or callback.

## 2. Define legal behavior

```python
from statemachine import State, StateChart

from sose.api import probabilistic_transitions


@probabilistic_transitions({}, excluded_events={"start", "finish"})
class TutorialJobChart(StateChart):
    queued = State(initial=True)
    running = State()
    completed = State(final=True)

    start = queued.to(running)
    finish = running.to(completed)
```

The StateChart answers **what is legal**. The example is deterministic, so both
events are excluded from stochastic selection and are dispatched explicitly.

## 3. Build the runtime from the public API

```python
from datetime import timedelta

from sose.api import (
    DomainRegistry,
    Engine,
    EntityType,
    RandomSource,
    Scheduler,
    SimulationClock,
    SimulationContext,
)

context = SimulationContext(
    clock=SimulationClock(now=ORIGIN, step=timedelta(hours=1)),
    random=RandomSource(root_seed=1701),
    scheduler=Scheduler(),
)

registry = DomainRegistry()
registry.register(EntityType("tutorial_job", TutorialJobChart))

engine = Engine(
    context=context,
    registry=registry,
    persistence=persistence,
)
```

Domain authors should prefer `sose.api` for compatibility-relevant concepts.
A concrete optional backend is imported explicitly because it is an execution
choice:

```python
from sose.backends.simpy import SimPyBackend
```

## 4. Persist the entity

Create through the context factory, then save it transactionally:

```python
job = context.entities.create(
    TutorialJob,
    key=("tutorial-job", "example"),
    state="queued",
    attributes={"job_key": "example"},
)

with persistence.transaction() as uow:
    uow.save_entity(job)
```

Factories give identities and timestamps deterministic runtime semantics.

## 5. Dispatch the immediate transition

Commands express intention. The engine applies the command through the bound
StateChart and persists the resulting state/event transactionally:

```python
start = engine.context.commands.create(
    "start",
    target=job,
    correlation_id=job.id,
    key=("tutorial-job", job.id, "start"),
)
engine.dispatch(start)
```

Do not assign `job.state = "running"` as process orchestration.

## 6. Schedule durable future meaning

Create a command for the future and submit it through
`context.schedules`:

```python
finish = engine.context.commands.create(
    "finish",
    target=job,
    due_at=complete_at,
    correlation_id=job.id,
    key=("tutorial-job", job.id, "finish"),
)
engine.context.schedules.at(complete_at, command=finish)
```

The durable scheduler persists the command and ScheduledWork. The backend merely
receives a reconstructible callback.

Avoid the lower-level compatibility-only `context.scheduler` queue for new
domain code.

## 7. Attach an ephemeral backend

```python
backend = SimPyBackend(origin=ORIGIN)
engine.rebuild_backend(backend)
backend.run_until(complete_at)
```

After the boundary, the durable entity is `completed` and the ScheduledWork is
consumed.

## 8. Prove restart semantics

The important test is not only that the happy path works. Rebuild the backend
*after* scheduling but *before* completion:

```python
rebuilt = restart_reference_runtime(
    persistence,
    build_runtime,
    backend,
    backend_factory=SimPyBackend,
)
rebuilt.backend.run_until(complete_at)
```

The result must be the same durable truth.

This is the core SOSE architecture:

```text
durable entity + command + ScheduledWork
            |
            | reconstruct
            v
      ephemeral backend
```

## 9. Move to a real persistence boundary

For a file-backed model:

```python
from sose.api import SQLitePersistence

persistence = SQLitePersistence("simulation.sqlite3")
```

The domain and engine code do not change.

## What to learn next

Once this minimal flow is clear:

- resources/contention: Manufacturing, Hospitals, Field Service;
- Store ownership: Insurance, Airports, Warehouse;
- preemption: Aviation;
- immutable occurrences: Telecom, Energy, Transit;
- interval ownership: Hospitality;
- future-effective intent: Subscription/SaaS;
- persistence contract: `docs/architecture/persistence-conformance.md`;
- public compatibility tiers: `docs/architecture/public-api.md`.

A new domain should introduce semantic pressure rather than duplicate an
existing Reference with different nouns.
