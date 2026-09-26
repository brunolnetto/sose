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


---

## Durable Store semantics — v0.7

Store execution mechanics remain backend-owned, but their semantic state is now
represented durably.

The durable records are:

- `StoreDefinition` — store name, kind, and optional capacity;
- `DurableStoreItem` — item accepted into the semantic store;
- `StorePutIntent` — put requested but not yet durably accepted;
- `StoreGetRequest` — outstanding consume request;
- `StoreGetResult` — terminal durable receipt containing the consumed item.

The lifecycle of a put is:

```text
put requested
    ↓ persist
StorePutIntent
    ↓ backend accepts
DurableStoreItem
```

The lifecycle of a get is:

```text
get requested
    ↓ persist
StoreGetRequest
    ↓ backend returns matching item
atomic transaction:
  delete DurableStoreItem / pending put
  delete StoreGetRequest
  persist StoreGetResult
```

The consume transaction removes both the item and request atomically and writes
the terminal result in the same commit. A replayed GET can therefore complete
after restart without a live callback and still preserve the consumed value.

Completed `request_id` values remain globally reserved by their
`StoreGetResult`. Reusing a completed ID is rejected before a new request is
persisted or submitted to the backend.

### Restart reconstruction

A fresh backend is reconstructed in two phases:

```text
1. create Store definitions
2. restore already-accepted items
3. merge pending put/get operations by durable sequence
4. replay pending operations in original semantic order
```

For `PriorityStore`, accepted items are restored by:

```text
(priority, original durable sequence)
```

so equal-priority order remains deterministic across restart.

Bounded stores keep blocked puts as `StorePutIntent` records. A crash therefore
does not lose a put merely because backend capacity was unavailable.

### FilterStore

Python callables are never persisted.

A durable filtered get stores:

```text
filter_key = "bearing"
```

The Engine receives a runtime registry mapping stable keys to predicates:

```python
Engine(
    ...,
    store_filters={
        "bearing": lambda item: item.value["kind"] == "bearing",
    },
)
```

Recovery fails before mutating resource/store reconstruction if a persisted
`filter_key` cannot be resolved.

This preserves the general SOSE rule:

```text
persist semantic identity of behavior
not Python continuation/code objects
```

### Crash boundaries

The durable protocol explicitly covers:

```text
StorePutIntent persisted
↓ crash before backend acceptance

backend accepts put
↓ crash before DurableStoreItem commit

StoreGetRequest persisted
↓ crash before matching item exists

backend matches item
↓ crash before consume commit
```

All of these states can be replayed into a fresh backend from persistence.

Native `simpy.Store`, `StorePut`, `StoreGet`, queues, callbacks, and filter
closures remain ephemeral execution mechanics.


---

## Durable preemption semantics — v0.7

Preemption is a semantic ownership change and must be crash-consistent. Native
`simpy.Interrupt`, `Preempted`, process objects, and request queues remain
ephemeral backend mechanics.

The durable records are:

- `PreemptiveResourceDefinition` — capacity;
- `PreemptiveResourceDemand` — pending request, priority, and preempt flag;
- `PreemptiveResourceReservation` — active semantic holder;
- `PreemptiveResourceReleaseIntent` — release committed semantically but not yet finalized;
- `ResourcePreemptionResult` — terminal causal record of holder displacement.

### Normal acquisition

```text
PreemptiveResourceDemand
    ↓ backend acquisition
PreemptiveResourceReservation
```

### Preemption handshake

A backend preemption produces two independent ephemeral observations:

```text
A. old holder receives preemption callback
B. new request receives acquisition callback
```

SOSE does not mutate durable truth after only one observation.

Instead, both callbacks are retained ephemerally until they can be paired by
`preempting_request_id`.

Only then does one durable transaction perform:

```text
delete old reservation
delete preempting demand
persist successor reservation
persist ResourcePreemptionResult
```

This prevents transient durable over-allocation.

### Crash between callbacks

If the process crashes after A or B but before the pair is complete, persistence
still contains:

```text
old reservation
+ preempting demand
```

The changed backend state dies with the process. Recovery reconstructs the last
valid durable truth and replays the demand, so the preemption can occur again.

### Terminal result

`ResourcePreemptionResult` records:

- displaced reservation ID;
- displaced request ID;
- preempting request ID;
- successor reservation ID;
- resource name;
- logical preemption time;
- durable sequence.

The result is saved only after the displaced reservation has been removed and the
successor reservation exists in the same transaction.

### Release while waiters exist

Manual release uses the same intent-first discipline as non-preemptive resources:

```text
active reservation
    ↓ persist release intent
reservation + release intent
    ↓ backend release
backend may synchronously grant waiter
    ↓
finalize holder removal
    ↓
commit deferred waiter grant
```

If the process crashes after backend release but before durable finalization,
recovery completes the release intent before replaying pending demands.

### Restart reconstruction

Recovery performs:

```text
validate durable preemptive state
↓
finalize interrupted releases
↓
create native PreemptiveResource definitions
↓
reconstruct active reservations
↓
materialize holders at current backend time
↓
replay pending demands by durable priority/sequence
```

Existing holders are materialized before pending higher-priority demands so that
a replayed urgent request exercises real preemption rather than bypassing the
durable holder through initial queue ordering.

### Recovery invariants

Before mutating runtime state, SOSE validates that:

- every demand and reservation references a known definition;
- active durable reservations do not exceed capacity;
- one request cannot be both demand and reservation;
- release intents reference live reservations on the same resource;
- preemption results reference known resource definitions.

The persistence layer additionally requires a preemption result to reference an
existing successor reservation while the displaced reservation is already gone.

The invariant remains:

```text
preemptive demand/reservation/result = durable semantic truth
simpy.PreemptiveResource / Process / Interrupt = ephemeral execution mechanics
```
