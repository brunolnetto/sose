# Resources and Queues

## Purpose

Many operational systems are constrained by finite resources.

Examples:

```text
technicians
machines
hospital beds
warehouse docks
vehicles
inspectors
cashiers
support agents
loading bays
production lines
```

Without resource constraints, simulations may generate valid lifecycle transitions while producing unrealistic operational throughput.

SOSE therefore models resources, capacity, reservations, and queues as explicit runtime concepts.

---

## Architectural responsibility

StateCharts define whether a lifecycle transition is legal.

Resources determine whether the operational system currently has the capacity to perform that transition.

Example:

```text
WorkOrder RELEASED
        ↓
StartWork command
        ↓
transition is legally valid
        ↓
technician available?
      /        \
    yes         no
     ↓           ↓
IN_PROGRESS   WAITING_RESOURCE
```

Resource availability is context.

It must not be hidden as arbitrary randomness.

---

## Resource

A `Resource` represents a constrained operational capability.

Conceptually:

```python
Resource(
    id=...,
    resource_type="technician",
    capacity=1,
    attributes={
        "skill": "electrical",
        "site": "plant-a",
    },
)
```

Resources may be:

```text
discrete
pooled
fungible
named
skill-constrained
location-constrained
```

---

## ResourcePool

A `ResourcePool` manages resources that may satisfy the same demand.

Example:

```text
ElectricalTechnicians
├── TECH-001
├── TECH-002
└── TECH-003
```

or a fungible capacity:

```text
WarehouseDockCapacity = 8
```

---

## Capacity

Capacity may vary over time.

Example:

```text
08:00–16:00 capacity = 10
16:00–00:00 capacity = 4
00:00–08:00 capacity = 1
```

Scenario effects may alter capacity:

```text
machine failure
    ↓
capacity - 1

staff shortage
    ↓
technician capacity × 0.7
```

---

## Reservation

A resource reservation represents temporary allocation.

Conceptually:

```python
Reservation(
    resource_id=...,
    entity_id=...,
    quantity=1,
    start_at=...,
    release_at=...,
)
```

Reservation semantics must be deterministic.

Two simultaneous requests cannot both acquire the same exclusive resource.

---

## Acquire / release lifecycle

Typical flow:

```text
Request resource
      ↓
available?
  ┌───┴────┐
 yes       no
  ↓         ↓
allocate   enqueue
  ↓
execute work
  ↓
release
  ↓
wake eligible queue item
```

The release operation may causally schedule new commands.

---

## Queues

A queue represents waiting demand.

Queue policies may include:

```text
FIFO
LIFO
priority
deadline-aware
shortest-processing-time
weighted priority
custom domain policy
```

Example:

```text
MaintenanceQueue
├── emergency WO-12
├── high WO-08
├── normal WO-15
└── normal WO-18
```

---

## QueueEntry

Conceptually:

```python
QueueEntry(
    entity_id=...,
    requested_at=...,
    priority=...,
    deadline=...,
    requirements=...,
)
```

Queue order must remain deterministic.

A queue policy should produce a total ordering when entries otherwise tie.

---

## Resource requirements

An operational action may require multiple resources.

Example:

```text
maintenance task
    requires:
      1 technician(skill=electrical)
      1 inspection kit
      1 work bay
```

All-or-nothing allocation may be necessary to avoid deadlocks.

---

## Deadlocks

Resource acquisition can introduce deadlocks.

Example:

```text
Task A holds machine, waits for technician
Task B holds technician, waits for machine
```

SOSE should avoid implicit partial allocation when a domain action requires an atomic resource set.

Future APIs may support:

```text
resource bundle request
```

that succeeds or fails atomically.

---

## Resource-aware probability

Resources can influence transition weights.

Example:

```text
P(start_work)
```

may increase when:

```text
technician available
material available
work bay available
```

The probabilistic graph reads resource context.

It does not own resource allocation.

---

## Resource-aware scheduling

Resources and scheduling interact.

Example:

```text
shipment ready
   ↓
truck unavailable
   ↓
queue shipment
   ↓
truck released at 14:00
   ↓
dispatch command scheduled
```

The Scheduler remains responsible for *when* follow-up work executes.

The Resource Manager remains responsible for *whether capacity exists*.

---

## State modeling

Waiting for resources may be explicit domain state:

```text
WAITING_TECHNICIAN
WAITING_BED
WAITING_DOCK
```

or may remain runtime scheduling state.

The choice depends on whether the waiting state is meaningful to the external operational system.

Rule:

> If a real source system would expose the waiting state, model it in the domain StateChart.

Otherwise, resource waiting may remain SOSE-private.

---

## Metrics

Resources enable useful simulation outputs:

```text
utilization
queue length
wait time
throughput
capacity saturation
resource idle time
deadline misses
SLA breaches
```

---

## Invariants

1. Resource allocation is deterministic.
2. Capacity cannot become negative.
3. Exclusive resources cannot be allocated concurrently.
4. Queue ordering is deterministic.
5. Resource state survives restart when durable persistence is enabled.
6. Scenario effects cannot silently violate capacity invariants.
7. Resource availability is context, not StateChart topology.
8. Domain-visible waiting states remain domain-owned.

---

## Future work

- skill matrices;
- geographic resources;
- setup/changeover time;
- preemption;
- multi-resource atomic reservations;
- deadlock detection;
- dynamic capacity;
- resource calendars;
- cost accounting;
- optimization-driven allocation policies.
