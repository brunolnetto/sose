# Airports Reference Domain Specification

## 1. Purpose and scope

This example models airport turnaround operations without collapsing gate,
ground-service, baggage, and departure-slot semantics into a single state machine.

Durable entities:

- `FlightTurnaround`;
- `GateAssignment`;
- `GroundServiceTask`;
- `BaggageFlow`;
- `DepartureSlot`.

## 2. Operational story

A scheduled FlightTurnaround arrives through durable time. It must acquire gate
capacity before deboarding and servicing. Ground service completes through its
own durable task and resource ownership. Baggage becomes ready independently and
may delay boarding.

A DepartureSlot is separately scheduled and becomes due through ScheduledWork.
After service and baggage are ready, the turnaround enters a priority departure
queue. Departure then requires the due/delayed slot, ownership of the head of the
priority queue, tug capacity, and normal operational availability.

The departure slot does not itself depart the flight.

## 3. StateCharts

FlightTurnaround:

    scheduled -> arrived
              -> gate_hold -> gate_assigned
              -> deboarding
              -> servicing
              -> boarding
                 -> waiting_baggage -> boarding
              -> waiting_slot
              -> pushback
              -> departed

GateAssignment:

    planned -> reserved -> occupied -> released

GroundServiceTask:

    pending -> in_progress -> completed
                           -> failed -> in_progress

BaggageFlow:

    pending -> transferring -> ready
                           -> delayed -> ready

DepartureSlot:

    planned -> scheduled -> due -> consumed
                           -> delayed -> consumed

All business transitions are orchestration-gated.

## 4. Durable operational model

Durable truth includes:

- entity lifecycle state and immutable DomainEvent history;
- ScheduledWork for arrival and departure-slot timing;
- ResourceDemand / ResourceReservation / ResourceReleaseIntent for gate,
  ground-team, and tug capacity;
- PriorityStore durable items and StoreGetResult selection evidence for departure ordering;
- deterministic correlation across turnaround, gate assignment, service,
  baggage, and slot entities;
- ScenarioRuntimeState;
- SimulationPosition.

Backend-native callbacks, SimPy Events, queue objects, handles, and generator
state are reconstructible mechanics and are never semantic truth.

## 5. Resources and queues

Resources:

- `gate`;
- `ground_team`;
- `tug`.

Queue:

- `departure_queue` is a PriorityStore ordered by departure priority.

The dispatcher validates the durable priority head before consuming an item, so
reconciling one turnaround cannot silently remove another flight's queue item.

## 6. Happy path

1. arrival ScheduledWork fires;
2. gate capacity is acquired;
3. GateAssignment becomes occupied;
4. turnaround deboards and enters servicing;
5. ground-team capacity executes GroundServiceTask;
6. baggage becomes ready;
7. turnaround joins the departure priority queue;
8. DepartureSlot becomes due;
9. tug capacity is acquired;
10. the turnaround owns the priority head;
11. slot is consumed;
12. turnaround departs;
13. tug and gate capacity are released.

## 7. Representative sad paths

### Gate congestion

Gate unavailability moves an arrived turnaround to gate_hold without leaving a
queued or granted gate demand behind. Normal assignment resumes after recovery.

### Baggage delay

BaggageFlow(delayed) moves the turnaround to waiting_baggage. Departure queueing
is illegal until baggage becomes ready again.

### Weather / slot delay

A due DepartureSlot becomes delayed while departure availability is false.
No tug demand is retained during the disruption. The delayed slot remains valid
for later consumption after recovery.

### Capacity contention

Gate and tug demand are durable and survive backend rebuild. Releasing a blocker
allows the original workflow to continue without duplicating semantic work.

## 8. Invariants

AIR-01 — Gate before turnaround service: a FlightTurnaround cannot enter
service before durable gate ownership is established.

AIR-02 — Service evidence before departure queue: GroundServiceTask must be
completed before queueing.

AIR-03 — Baggage evidence before departure queue: BaggageFlow must be ready.

AIR-04 — Slot time is not departure authority: ScheduledWork may make
DepartureSlot(due), but departure still requires queue ownership and tug capacity.

AIR-05 — Priority ownership: a turnaround may consume only the current durable
head of the departure PriorityStore.

AIR-06 — Slot consumption is single-use durable evidence.

AIR-07 — Post-commit cleanup: if business state commits before resource cleanup,
reconciliation must still release capacity after retry/restart.

AIR-08 — Scenario discipline: congestion/weather scenarios change prerequisite
availability, not lifecycle state directly.

AIR-09 — Restart equivalence: continuous and rebuilt runs converge across
arrival schedule, gate demand, tug demand, and consumed-slot/pushback boundaries.

## 9. Restart semantics

Executable recovery boundaries:

1. arrival ScheduledWork pending;
2. gate ResourceDemand queued behind another owner;
3. tug ResourceDemand queued behind another owner;
4. GateAssignment(occupied) before turnaround gate transition;
5. GroundServiceTask(completed) before turnaround service completion;
6. committed departure StoreGetResult before slot_ready;
7. DepartureSlot(consumed) + FlightTurnaround(pushback) before depart.

## 10. Scenarios

`gate_congestion_scenario` temporarily removes gate availability.

`weather_departure_scenario` temporarily removes departure availability.

Both scenarios are finite. Recovery occurs through ordinary reconciliation.

## 11. Executable evidence

| Specification area | Evidence | Status |
|---|---|---|
| durable entities | entities.py + topology tests | implemented |
| StateCharts | statecharts.py + tests | implemented |
| durable arrival timing | ScheduledWork restart test | implemented |
| gate ownership / reallocation | Resource + congestion/reallocation/restart tests | implemented |
| ground service | Resource + crash-recovery tests | implemented |
| baggage delay/recovery | happy/sad path tests | implemented |
| departure slot timing | ScheduledWork + weather tests | implemented |
| priority departure queue | PriorityStore + head-ownership + committed-selection restart tests | implemented |
| tug capacity | Resource + restart test | implemented |
| slot-consume crash recovery | pushback restart test | implemented |
| illegal prerequisites | departure-queue invariant tests | implemented |
| finite scenarios | gate/weather tests | implemented |

## Promotion decision

Current status: **Partial**.

Promotion awaits CI and final audit of the exact branch.
