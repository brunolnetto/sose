# Aviation Reference Domain Specification

## 1. Purpose and scope

This example models an aircraft rotation as durable causal truth across flight
legs, aircraft availability, crew, inspection, parts, and maintenance.

Durable entities:

- `Aircraft`;
- `Flight`;
- `CrewAssignment`;
- `Inspection`;
- `MaintenanceWorkOrder`;
- `PartDemand`.

The reference rotation contains two legs sharing one Aircraft. Leg 2 names Leg 1
as its predecessor and cannot become operationally ready until Leg 1 is released.

## 2. Operational story

Each Flight has a durable departure time. ScheduledWork makes a flight due, but
does not dispatch it. Dispatch still requires:

- predecessor release when applicable;
- departure availability;
- crew availability and capacity;
- Aircraft available/released state.

Landing releases the flight crew but not the Aircraft. Flight and Aircraft enter
inspection. A passed Inspection releases both. A failed Inspection marks the
Aircraft AOG and creates a distinct MaintenanceWorkOrder and PartDemand.

AOG maintenance requires a spare-part issue, durable priority-queue selection,
and preemptive maintenance-bay capacity. The AOG request may preempt routine
maintenance. Completing maintenance releases the Aircraft and the affected
Flight, which then allows downstream legs to resume.

## 3. StateCharts

Flight:

    scheduled -> due -> ready -> airborne -> landed -> inspection -> released
                   -> delayed -> due
    airborne -> diverted
    scheduled/due/delayed -> cancelled

Aircraft:

    available -> assigned -> airborne -> inspection -> released
                                      -> aog -> maintenance -> released

CrewAssignment:

    planned -> reserved -> active -> released

Inspection:

    pending -> inspecting -> passed
                          -> failed

MaintenanceWorkOrder:

    planned -> released -> waiting_part -> released
                        -> waiting_bay -> in_progress -> completed
                                               -> interrupted -> in_progress

PartDemand:

    open -> allocated -> issued

All transitions are orchestration-gated.

## 4. Durable operational ownership

Durable semantic truth includes:

- lifecycle state and immutable DomainEvent history;
- ScheduledWork for departure eligibility;
- ResourceDemand / ResourceReservation for flight crew and inspection team;
- PreemptiveResourceDemand / Reservation / PreemptionResult for maintenance bay;
- PriorityStore items and StoreGetResult for maintenance-work and part selection;
- deterministic IDs for Inspection, MaintenanceWorkOrder, and PartDemand;
- predecessor-flight identity for rotation causality;
- ScenarioRuntimeState and SimulationPosition.

Backend-native events, callbacks, generators, native queues, and resource handles
remain reconstructible mechanics.

## 5. Happy path

1. Leg 1 departure schedule becomes due.
2. Crew is reserved and capacity acquired.
3. Aircraft is assigned.
4. Leg 1 and Aircraft become airborne.
5. Leg 1 lands; crew is released.
6. Inspection passes.
7. Leg 1 and Aircraft are released.
8. Leg 2 becomes due.
9. Predecessor release gate is satisfied.
10. Leg 2 acquires crew and the same Aircraft.
11. Leg 2 departs.

## 6. Delay propagation

If Leg 2 becomes due while Leg 1 is not released, Leg 2 becomes delayed. The
delay is not copied as a string or timestamp from Leg 1; it follows from the
durable predecessor-release invariant. Once Leg 1 and Aircraft are released,
normal reconciliation resumes Leg 2.

Weather and crew-shortage scenarios use the same delayed state but affect only
prerequisite availability.

## 7. AOG maintenance path

1. Inspection fails.
2. Aircraft becomes AOG.
3. MaintenanceWorkOrder and PartDemand are created.
4. Work order enters durable maintenance priority queue.
5. Work waits for PartDemand(issued).
6. Part selection commits as StoreGetResult.
7. Work enters waiting_bay.
8. AOG request acquires/preempts maintenance_bay at high priority.
9. Work and Aircraft enter maintenance.
10. Work completes.
11. Aircraft and Flight are released.
12. Downstream rotation may resume.

## 8. Invariants

AVI-01 — Departure time is eligibility, not dispatch authority.

AVI-02 — A downstream leg requires predecessor Flight(released).

AVI-03 — Aircraft ownership is independent durable truth from Flight state.

AVI-04 — Crew capacity must be acquired before departure.

AVI-05 — Landing releases crew but not airworthiness.

AVI-06 — Flight release requires Inspection(passed) or completed AOG maintenance.

AVI-07 — Failed Inspection evidence is immutable and creates separate maintenance
facts rather than rewriting inspection history.

AVI-08 — Part before bay: AOG maintenance cannot request scarce maintenance-bay
capacity until PartDemand is issued.

AVI-09 — Maintenance queue ownership is durable through StoreGetResult.

AVI-10 — AOG maintenance may preempt lower-priority routine maintenance only
through committed ResourcePreemptionResult evidence.

AVI-11 — Scenario discipline: weather/crew scenarios affect prerequisite
availability rather than business state directly.

AVI-12 — Restart equivalence holds across scheduled departure, crew demand,
maintenance queue selection, maintenance capacity, and post-completion release.

## 9. Crash/restart boundaries

Executable recovery includes:

1. departure ScheduledWork pending;
2. crew ResourceDemand queued behind another owner;
3. Flight(airborne) committed before Aircraft(dispatch);
4. Inspection(passed) before Flight/Aircraft release;
5. Inspection(failed) before Aircraft(AOG)/maintenance creation;
6. maintenance StoreGetResult committed after queue removal;
7. MaintenanceWorkOrder(completed) before Aircraft/Flight release;
8. preemptive maintenance capacity reconstructed after backend rebuild.

## 10. Scenarios

`departure_weather_scenario` temporarily removes departure availability.

`crew_shortage_scenario` temporarily removes crew availability.

Both are finite; recovery occurs through the ordinary reconciliation path.

## 11. Executable evidence

| Specification area | Evidence | Status |
|---|---|---|
| durable entities | entities.py + topology tests | implemented |
| StateCharts | statecharts.py + tests | implemented |
| departure schedule | ScheduledWork + restart test | implemented |
| crew capacity | Resource + scenario/restart tests | implemented |
| aircraft/flight causal dispatch | runtime + crash test | implemented |
| two-leg delay propagation | happy-path rotation test | implemented |
| inspection pass/fail | happy/crash tests | implemented |
| AOG part gating | part issue tests | implemented |
| maintenance priority queue | PriorityStore + restart test | implemented |
| preemptive maintenance bay | AOG preemption test | implemented |
| maintenance completion recovery | restart test | implemented |
| finite scenarios | weather/crew tests | implemented |

## Promotion decision

Current status: **Reference implementation**.

Promotion is based on executable evidence for two-leg causal rotation, durable departure timing, crew/aircraft ownership, inspection pass/fail, AOG part gating, priority maintenance selection, committed preemption evidence, downstream AOG delay propagation, post-commit dispatch/landing/inspection/maintenance reconciliation, and restart equivalence across ScheduledWork, ResourceDemand, StoreGetResult, PreemptiveResourceReservation, and maintenance-completion boundaries.
