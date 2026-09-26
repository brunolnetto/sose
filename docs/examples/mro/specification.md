# Maintenance / MRO Domain Specification

## Status

**Partial implementation under the reference-domain standard.**

The repository contains a persistent `WorkOrder` entity, an explicit StateChart,
probabilistic transition metadata, durable scheduling, transition events, and tests
showing scheduled execution semantics.

It does **not yet** contain a complete MRO reference vertical slice with technicians,
maintenance bays, spare-parts inventory, waiting-resource behavior, emergency priority,
full happy/sad process execution, MRO-specific scenarios, and domain-level restart
equivalence.

This specification therefore serves two purposes:

1. document exactly what the current MRO example means;
2. define the missing executable behavior required to promote it to a reference
   implementation.

## 1. Purpose and scope

The intended MRO domain models maintenance work from planning through execution and
closure, including material/resource constraints and exceptional maintenance behavior.

### Currently executable

- one persistent `WorkOrder`;
- WorkOrder StateChart;
- durable scheduled `release`;
- scheduled transition execution;
- immutable transition events;
- durable logical-time advancement;
- stale scheduled-callback idempotence.

### Defined by the StateChart but not yet implemented as a complete domain process

- waiting for material;
- transition into active work;
- completion;
- closure;
- cancellation;
- probabilistic branch selection across applicable transitions.

### Required before reference-grade promotion

- technician/resource acquisition;
- maintenance-bay/equipment capacity where applicable;
- spare-parts demand and inventory;
- explicit waiting-resource behavior;
- material shortage/replenishment;
- emergency/priority maintenance;
- representative cancellation/reopen or interruption path;
- scenario intervention;
- continuous-versus-restarted domain equivalence across happy and sad paths.

## 2. Operational story

The intended process begins with a planned work order.

A planned work order may be released for execution or cancelled.

Once released, the work order may start when operational prerequisites are available,
or it may wait for material. The StateChart permits a waiting-material work order to
start once the constraint is resolved.

Active work becomes completed, and completed work is then closed.

The current demo implements only the beginning of this story: it creates a planned
work order, schedules `release`, and proves that durable scheduled work transitions the
entity to `released` exactly once.

Therefore the lifecycle below is the domain contract, while only a subset currently
has executable process orchestration.

## 3. Domain entity

### WorkOrder

**Responsibility**

Represents a maintenance job and owns its business lifecycle.

**Relevant attributes in the current demo**

- `priority`.

**Owns**

- lifecycle state from planning through closure/cancellation.

**Does not currently own or model**

- technician assignment;
- bay/equipment capacity;
- spare-parts inventory;
- asset availability;
- material-demand entity;
- repair output/return-to-service state.

Those are required components of the future full reference slice rather than hidden
attributes of the WorkOrder.

## 4. WorkOrder StateChart

```text
planned
  ├──release──> released
  │               ├──start──────────────> in_progress
  │               ├──wait_for_material─> waiting_material
  │               │                        │
  │               │                        └──start──> in_progress
  │               └──cancel─────────────> cancelled
  │
  └──cancel──────────────────────────────> cancelled

in_progress
  │ complete
  ▼
completed
  │ close
  ▼
closed
```

`cancel` is also legal from `waiting_material`.

| Current state | Command | Domain meaning | Next state | Current executable process evidence |
|---|---|---|---|---|
| `planned` | `release` | authorize maintenance execution | `released` | yes |
| `planned` | `cancel` | cancel before release | `cancelled` | StateChart only |
| `released` | `start` | begin active maintenance | `in_progress` | StateChart; generic scheduled test exercises transition |
| `released` | `wait_for_material` | required spare part unavailable | `waiting_material` | StateChart only |
| `waiting_material` | `start` | material constraint resolved | `in_progress` | StateChart only |
| `released` / `waiting_material` | `cancel` | terminate outstanding work | `cancelled` | StateChart only |
| `in_progress` | `complete` | maintenance work finished | `completed` | StateChart only |
| `completed` | `close` | administratively close work | `closed` | StateChart only |

### Probabilistic transition metadata

The current `WorkOrderChart` declares transition weights:

```text
release           0.98
start             0.78
wait_for_material 0.20
cancel            0.02
complete          1.00
close             1.00
```

These values are part of the model metadata. The current MRO demo does not yet provide
a complete operational experiment demonstrating these branches as a reference process.

## 5. Current executable process

### 5.1 Implemented demo path

```text
create WorkOrder(planned)
    │
    ├──persist entity
    │
    └──persist ScheduledWork(release)
             │
             ▼
       advance simulation
             │
             ▼
       release dispatched
             │
             ├──WorkOrder(released)
             ├──scheduled work consumed
             ├──transition event persisted
             └──SimulationPosition advanced
```

This is a durable scheduling demonstration using an MRO entity. It is not yet a
complete MRO happy path.

### 5.2 Intended reference happy path

The future reference-grade process should be:

```text
WorkOrder(planned)
  │ release
  ▼
released
  │
  ├── acquire technician
  ├── acquire maintenance capacity
  └── verify / reserve required spare parts
          │
          ▼
      in_progress
          │
          ├── durable parts consumption
          └── durable repair/service effect
                  │
                  ▼
              completed
                  │ inspection / return-to-service evidence
                  ▼
                closed
```

## 6. Required sad-path specifications

The following paths are required before MRO can be promoted back to
**Reference implementation**.

### 6.1 Spare-part shortage

**Trigger**

Required maintenance material is unavailable.

**Required behavior**

```text
released
  │ wait_for_material
  ▼
waiting_material
  │ replenishment / allocation
  ▼
in_progress
```

**Required durable truth**

- material demand remains explicit;
- no part is partially consumed while quantitative availability is insufficient;
- restart preserves the wait/replenishment relationship.

### 6.2 Technician or bay contention

**Trigger**

Required maintenance capacity is unavailable.

**Required behavior**

The work order remains unable to claim `in_progress` until required durable
reservations exist.

### 6.3 Emergency maintenance / priority displacement

**Trigger**

A higher-priority work order requires constrained maintenance capacity.

**Required behavior**

If domain policy permits preemption, displacement must be represented by durable
preemption evidence and the displaced work must remain business-visible.

### 6.4 Cancellation

**Trigger**

Outstanding maintenance work is cancelled while planned, released, or waiting.

**Required behavior**

Cancellation must release/avoid capacity and inventory effects and become terminal.

### 6.5 Interruption / reopen decision

A production-grade MRO domain normally needs a policy for work that is interrupted,
fails inspection, or must be reopened. The current StateChart does not model a
`reopened` state. This must be decided explicitly rather than invented implicitly by
orchestration code.

## 7. Commands and domain events

| Command | Target | Meaning |
|---|---|---|
| `release` | WorkOrder | authorize maintenance work |
| `wait_for_material` | WorkOrder | expose material constraint |
| `start` | WorkOrder | begin active maintenance |
| `complete` | WorkOrder | mark work execution complete |
| `close` | WorkOrder | administratively close completed work |
| `cancel` | WorkOrder | terminate outstanding work |

Successful commands emit immutable `entity.state_transition` events.

The current demo proves the release transition is durably scheduled and committed.
A future full reference slice should use stable correlation across work order,
resource allocation, material demand, repair effects, and inspection/return-to-service.

## 8. Invariants

### Currently evidenced

**MRO-01 — Durable scheduled transition**

Scheduled lifecycle work is persisted before execution and consumed atomically with a
successful transition.

**MRO-02 — Stale callback idempotence**

A stale callback for already-consumed scheduled work cannot execute the same transition
twice.

**MRO-03 — Logical-time consistency**

Executing scheduled work advances durable logical time to the scheduled instant.

**MRO-04 — Failed scheduled dispatch rollback**

A failed scheduled dispatch leaves the command/work pending and does not advance
durable logical time.

### Required for reference-grade promotion

**MRO-05 — Resource gating**

A WorkOrder cannot enter `in_progress` before required technician/capacity reservations
exist.

**MRO-06 — Parts before repair**

A WorkOrder cannot claim active/completed physical repair when required spare-parts
effects are not durable.

**MRO-07 — Shortage visibility**

Spare-part shortage must remain explicit durable semantic state.

**MRO-08 — No duplicate repair/material effects**

Restart/retry cannot duplicate parts consumption or repair completion.

**MRO-09 — Priority/preemption visibility**

Emergency displacement, if supported, must remain observable in durable state.

**MRO-10 — Happy and sad restart equivalence**

Continuous and restarted execution must be semantically equivalent for the canonical
happy path and representative sad paths.

## 9. Durable truth and ownership

### Current example

| Concept | Durable owner |
|---|---|
| WorkOrder lifecycle | `WorkOrder.state` |
| scheduled release | Command + ScheduledWork |
| transition audit | DomainEvent |
| logical recovery position | SimulationPosition |

### Required full reference slice

| Concept | Expected durable owner |
|---|---|
| technician/bay demand and ownership | Resource demand/reservation |
| spare-part identity | Store |
| spare-part quantity | Container or explicit inventory entity |
| material shortage | WorkOrder + MaterialDemand lifecycle |
| emergency displacement | ResourcePreemptionResult |
| scenario activation | ScenarioRuntimeState |
| repair/inspection evidence | explicit durable result/event |

Backend-native scheduling callbacks, queues, resource handles, and SimPy objects remain
ephemeral and reconstructible.

## 10. Restart semantics

### Currently evidenced

The engine tests demonstrate:

- scheduled WorkOrder release executes exactly once;
- consumed work ignores stale callbacks;
- rescheduled replacements cannot be consumed by stale callbacks;
- logical time is committed with scheduled transitions.

### Required MRO domain-level restart gates

A full reference implementation must additionally compare continuous and restarted
execution across:

1. technician/resource queue;
2. spare-part shortage;
3. material allocation/consumption;
4. active maintenance;
5. emergency priority/preemption;
6. completion before closure;
7. at least one representative cancellation/interruption path.

## 11. Scenario specification

No MRO-specific scenario is currently registered by `build_demo()`.

A reference-grade MRO example should include at least:

### Asset failure / emergency arrival

- finite or one-shot trigger;
- creates/activates emergency maintenance pressure;
- must route through normal resource/preemption semantics.

### Spare-parts disruption

- changes availability/lead-time context;
- must not directly mutate WorkOrder state.

### Technician capacity loss

- changes effective capacity context;
- waiting-resource behavior remains explicit.

## 12. Example runs

### A. Current executable demo

```text
08:00 WorkOrder(planned)
08:00 durable release scheduled
tick / scheduled execution
→ WorkOrder(released)
→ transition event
→ scheduled work consumed
```

### B. Target nominal reference run

```text
planned
→ released
→ technician + bay acquired
→ spare parts allocated
→ in_progress
→ repair effect committed
→ completed
→ inspection / return to service
→ closed
```

### C. Target material-shortage run

```text
released
→ part unavailable
→ waiting_material
→ replenishment
→ part allocation
→ in_progress
→ completed
→ closed
```

### D. Target emergency-priority run

```text
normal work owns constrained resource
→ emergency work arrives
→ durable priority/preemption decision
→ displaced work remains explicit
→ emergency completes/releases
→ normal work resumes
```

## 13. Executable evidence and gaps

| Specification area | Current implementation/evidence | Status |
|---|---|---|
| WorkOrder entity | `entities.py` | implemented |
| WorkOrder StateChart | `statecharts.py` | implemented |
| durable scheduled release | `simulation.py::build_demo` | implemented |
| scheduled transition atomicity | `test_durable_engine.py` | implemented |
| stale callback idempotence | `test_durable_engine.py` | implemented |
| logical-time semantics | `test_durable_engine.py` | implemented |
| complete happy path | no MRO-specific vertical slice | missing |
| technician/bay resources | none in MRO example | missing |
| spare-parts inventory | none in MRO example | missing |
| waiting-material executable process | StateChart only | missing |
| emergency/preemption path | none in MRO example | missing |
| MRO scenarios | none | missing |
| happy-path restart equivalence | none at domain level | missing |
| sad-path restart equivalence | none at domain level | missing |

## Promotion decision

Under the repository's current reference-domain standard, MRO must remain **Partial**
until the missing executable evidence above is added.

The StateChart is a useful foundation, but a StateChart plus durable scheduling is not
equivalent to a complete operational reference domain.
