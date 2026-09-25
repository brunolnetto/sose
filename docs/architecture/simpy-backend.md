# Simulation Backends and SimPy

## Purpose

SOSE v0.5 introduces a backend boundary between **operational simulation semantics** and **discrete-event execution mechanics**.

The first backend is powered by SimPy.

The architectural rule is:

```text
SOSE
    = semantic operational simulation framework

SimPy
    = in-memory discrete-event execution backend
```

SimPy is not the domain model, persistence model, or public programming model of SOSE.

---

## Ownership boundary

### SOSE owns

```text
entities
StateCharts
commands
domain events
probabilistic transition semantics
scenarios
deterministic randomness
deterministic identities
causality / correlation
durable schedules
persistence / replay
source-system interfaces
process-mining semantics
```

### Backend owns

```text
in-memory event ordering
numeric discrete-event time
delayed callbacks
resource contention
resource wait queues
resource acquisition / release mechanics
```

### Adapter owns

```text
datetime <-> numeric backend time
SOSE scheduling handles <-> backend events
SOSE resource requests <-> backend requests
backend instrumentation
exception translation
```

---

## Public backend contracts

The domain-facing contracts live in:

```text
sose.backends.base
```

and expose:

```text
TemporalBackend
ResourceBackend
SimulationBackend

ScheduledCall
ResourceRequest
ResourceLease
ResourceSnapshot
```

No public contract exposes:

```text
simpy.Environment
simpy.Event
simpy.Process
simpy.Resource
yield
Python generator stacks
```

This is intentional.

---

## Temporal model

SOSE uses semantic `datetime` values.

SimPy uses a numeric timeline.

`SimPyBackend` defines an explicit origin:

```python
backend = SimPyBackend(
    origin=datetime(2026, 1, 1, tzinfo=timezone.utc),
)
```

The adapter converts:

```text
SOSE datetime
        ↓
seconds since origin
        ↓
SimPy numeric time
```

and converts back when exposing `backend.now`.

The SimPy numeric clock is never part of the domain contract.

---

## Scheduling

```python
call = backend.schedule_after(
    timedelta(hours=4),
    callback,
    priority=100,
    key=("work-order", work_order_id, "inspection"),
)
```

The returned `ScheduledCall` is backend-neutral.

Same-time callbacks are ordered by:

```text
backend priority
then scheduling sequence
```

Lower numeric priority executes first.

SOSE deterministic identities may be supplied through an explicit semantic `key`.

---

## Boundary execution

`run_until(at)` processes **all events whose timestamp is less than or equal to the requested boundary**.

This intentionally differs from relying directly on:

```python
env.run(until=t)
```

because SOSE wants an explicit inclusive boundary contract.

Conceptually:

```text
while next_event_time <= target:
    process next event

advance clock to target
```

---

## Cancellation

v0.5 supports callback cancellation through SOSE handles:

```python
backend.cancel(call)
```

Cancellation is an adapter concern.

The backend may retain a cancelled native event internally until its scheduled time, but its callback will not execute.

Durable cancellation semantics belong to v0.6.

---

## Resources

The v0.5 SimPy backend uses priority resources behind a backend-neutral API.

```python
backend.create_resource(
    "technicians",
    capacity=3,
)
```

A request is expressed as:

```python
request = backend.request_resource(
    "technicians",
    request_id="WO-123",
    priority=10,
    on_acquired=handle_lease,
)
```

The domain receives a `ResourceLease`, not a SimPy request object.

---

## Resource ordering

Resource capacity and waiting mechanics are provided by the backend.

Business priority remains a SOSE/domain decision.

For example:

```text
emergency maintenance  priority = 1
planned maintenance    priority = 100
```

The backend faithfully enforces those priorities.

SOSE does not ask SimPy to derive business priority.

---

## Resource snapshots

```python
snapshot = backend.resource_snapshot("technicians")
```

returns:

```text
capacity
in_use
queued
```

This creates a backend-independent observability surface suitable for scenarios and metrics.

---

## Why callbacks instead of generators in the public API

SimPy processes are naturally expressed as Python generators.

SOSE deliberately does not expose them.

A generator suspended inside:

```python
yield env.timeout(...)
```

contains runtime continuation state that is difficult to persist and reconstruct.

SOSE requires:

```text
persist
stop process
restart later
rebuild runtime
continue deterministically
```

Therefore:

```text
SimPy process state
    = ephemeral implementation state

SOSE state
    = durable semantic state
```

The v0.5 backend API uses explicit callbacks and handles so that v0.6 can reconstruct pending work from durable SOSE records.

---

## Persistence boundary

Never treat this as the persistence model:

```text
pickle(SimPy Environment)
```

The intended future flow is:

```text
durable SOSE state
├── entities
├── commands
├── scheduled work
├── scenario activations
├── resource reservations
└── execution position
        ↓
restart
        ↓
new backend instance
        ↓
reconstruct ephemeral events/resources
```

v0.5 does not yet implement that reconstruction.

---

## Relationship to the existing Scheduler

The existing heap-based `Scheduler` remains part of the current kernel.

v0.5 does not silently replace it.

Instead:

```text
v0.5
    define backend contract
    validate SimPy execution semantics

v0.6
    define durable scheduled-work model
    reconstruct backend from persistence
    integrate Engine execution with backend
```

This avoids a flag-day migration and preserves current tests.

---

## Optional dependency

SimPy is installed through:

```bash
pip install "sose[simpy]"
```

Development environments install it as part of the dev dependencies.

The base SOSE package can still be imported without SimPy.

Importing `sose.backends.simpy` without the optional dependency raises an explicit installation error.

---

## v0.5 invariants

1. No SimPy native object is part of the backend-neutral public API.
2. Domain code does not use `yield` or SimPy processes.
3. SOSE datetimes are converted to a backend numeric timeline only inside the adapter.
4. Same-time callbacks have deterministic priority/order semantics.
5. `run_until()` includes events exactly at the boundary.
6. Resources expose SOSE requests, leases, and snapshots.
7. Business priority is supplied by SOSE/domain code.
8. SimPy state is ephemeral.
9. Durable execution/reconstruction is deferred to v0.6.
10. The existing SOSE scheduler remains valid during migration.

---

## Deliberately deferred

v0.5 does not yet provide:

- persistent scheduled calls;
- persistent resource requests or leases;
- resource-request cancellation;
- preemptive resources;
- Store / FilterStore / Container abstractions;
- calendar-aware time;
- scenario-driven capacity mutation;
- Engine replacement of the current scheduler;
- restart reconstruction;
- realtime execution.

These capabilities should be added only after the durable execution model is defined.
