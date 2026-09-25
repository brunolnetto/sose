# Durable runtime architecture — v0.6

SOSE v0.6 makes the semantic runtime durable without serializing backend-native
objects such as SimPy environments, events, requests, generators, or callbacks.

The core invariant is:

```text
persistent semantic state = authoritative
backend execution state   = ephemeral and reconstructible
```

## Durable state

The recovery boundary is represented by durable semantic objects:

- `ScheduledWork` — future command execution intent.
- `SimulationPosition` — logical time, tick, execution sequence, and committed sequence.
- `ScenarioRuntimeState` — scenario decisions and active effects.
- `ResourceDefinition` — resource name and capacity.
- `ResourceDemand` — pending resource acquisition intent.
- `ResourceReservation` — durable ownership of acquired capacity.
- `ResourceReleaseIntent` — crash-safe release protocol marker.

Commands referenced by `ScheduledWork` are persisted separately so the backend
queue is never authoritative.

## Reconstruction

A fresh process/backend is rebuilt from persistence:

```text
Persistence
    ↓
RuntimeRebuilder
    ├── validate recovery boundary
    ├── validate pending scheduled work
    ├── restore logical time and tick
    ├── restore scenario runtime state
    ├── rebuild resources
    └── enqueue pending scheduled work
    ↓
fresh ephemeral backend
```

Recovery validation happens before mutating reconstruction phases. If the
persisted state is inconsistent with the requested recovery boundary, rebuild
fails without partially rebuilding resources.

## Durable scheduling

Domain code continues to use `context.schedules`. Once the context is attached
to an `Engine`, the factory is bound to `DurableScheduler`:

```text
ScheduleFactory
    ↓
DurableScheduler
    ↓
Persistence
    ↓
active backend, when attached
```

Schedules created while a backend is attached are both persisted and enqueued
for live execution. Schedules created without an attached backend remain
durable and are consumed by logical-tick execution or reconstructed later.

Ticks are fixed intervals, but recovery positions are not restricted to tick
boundaries.

There are two execution paths:

- During `Engine.advance_tick()`, durable work may execute at arbitrary
  timestamps inside the interval, while the transaction commits
  `SimulationPosition.logical_time` at the tick boundary.
- During live backend callback execution through `Engine.dispatch_scheduled()`,
  the callback commits `SimulationPosition.logical_time` at the scheduled
  work's exact `due_at`, which may be between tick boundaries.

Therefore every persisted `SimulationPosition.logical_time` is a valid
recovery boundary, whether tick-aligned or event-time-aligned.

## Resource durability

Resource lifecycle:

```text
ResourceDemand
    ↓ backend grant
ResourceReservation
    ↓ release requested
ResourceReservation + ResourceReleaseIntent
    ↓ backend release / restart recovery
released
```

The release intent is persisted before the backend release occurs. This closes
the crash window where a synchronous waiter promotion could otherwise leave
both the old holder and promoted waiter durably reserved.

During recovery, interrupted releases are finalized before surviving
reservations and pending demands are reconstructed.

## Restart equivalence

The architectural correctness target is observational equivalence between:

```text
continuous execution
```

and:

```text
execute
→ crash
→ rebuild fresh backend/context
→ continue
→ crash
→ rebuild again
→ continue
```

Durable equivalence covers entity state, domain events, scheduled work,
simulation position, scenario runtime state, resource definitions, demands,
reservations, and release intents.

Backend-native callbacks, lease objects, request objects, and queue internals are
not part of the equivalence contract; they are reconstructible execution
mechanics.

## What is intentionally not persisted

SOSE never persists:

- `simpy.Environment`
- `simpy.Process`
- Python generators
- native SimPy event queues
- callback closures
- backend-native lease/request handles

Persisting those objects would couple semantic truth to one execution backend
and make restart portability impossible.

## v0.6 boundary

v0.6 establishes durable discrete-event execution and restart reconstruction.
Future milestones may extend operational abstractions or add richer persistence
adapters, but they must preserve the same rule:

> backend state is reconstructible; durable semantic state is authoritative.
