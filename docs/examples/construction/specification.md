# Construction Reference Domain Specification

## 1. Purpose and scope

This example models a construction Activity from planning through dependency release, material
readiness, crew/equipment acquisition, execution, inspection, measurement and completion.
A separate ConstructionInspection represents each acceptance occurrence so a failed inspection
remains immutable evidence after rework and a later reinspection succeeds. A
ConstructionMeasurement is immutable completion evidence rather than a silent Activity attribute.

The domain is a composition reference: dependency truth, material truth, resource ownership,
inspection evidence and measurement evidence remain separate durable facts.

Initial scope:

- ConstructionActivity, ConstructionInspection and ConstructionMeasurement durable entities;
- predecessor dependency gating;
- material shortage/readiness;
- crew and equipment capacity;
- execution and inspection;
- rework after failed inspection;
- measurement evidence before completion;
- weather and procurement-delay scenarios.

## 2. Operational story

An Activity starts planned. If a predecessor is incomplete it moves to blocked_dependency;
dependency_ready is legal only when durable predecessor evidence exists. A released Activity may
wait for material, then request crew/equipment. Execution cannot start until both required
capacity claims are durable.

After work finishes, the Activity waits in inspection. Each ConstructionInspection is a distinct
durable occurrence. A passed inspection permits acceptance; a failed inspection remains terminal
evidence and routes the Activity to rework. Rework reacquires execution capacity and requires a
new inspection occurrence.

After acceptance the Activity is measured. Completion is gated by a correlated immutable
ConstructionMeasurement entity; measurement is not modeled as a meaningless StateChart self-loop.

## 3. Domain entities

### 3.1 ConstructionActivity

States: planned, blocked_dependency, ready, waiting_material, waiting_resource, executing,
inspection, rework, measured, completed, cancelled.

### 3.2 ConstructionInspection

States: pending, inspecting, passed, failed, voided.

`passed` and `failed` are terminal for one inspection occurrence. Reinspection after rework creates
a new deterministic correlated inspection identity.

### 3.3 ConstructionMeasurement

A single immutable `recorded` evidence entity stores the measured value for the Activity.
Completion requires a positive correlated ConstructionMeasurement; no mutable measurement flag is
used as the authoritative completion proof.

## 4. Durable operational model

Target reference-grade durable truth:

- Activity / Inspection / Measurement entities;
- Command + DomainEvent history;
- ScheduledWork for the durable planned-start milestone and other milestone/dependency timing;
- ResourceDemand / ResourceReservation / ResourceReleaseIntent for crew/equipment/inspector;
- Store/Container truth for material identity and quantity;
- ScenarioRuntimeState;
- SimulationPosition.

Suggested names:

- Resources: `crew`, `equipment`, `inspector`;
- Store: `material_lots`;
- Container: `material_quantity`.

## 5. StateCharts

Nominal Activity path:

    planned -> ready -> waiting_resource -> executing -> inspection -> measured -> completed

Dependency/material/rework branches:

    planned -> blocked_dependency -> ready
    ready -> waiting_material -> ready
    inspection -> rework -> waiting_resource -> executing -> inspection

Inspection occurrence:

    pending -> inspecting -> passed
                         \-> failed

All transitions are orchestration-gated.

## 6. Process specifications

### Happy path

1. predecessor evidence permits release;
2. material identity + quantity are available;
3. crew/equipment capacity is acquired;
4. execution starts and finishes;
5. inspection occurrence passes;
6. acceptance moves Activity to measured;
7. durable measurement evidence is recorded;
8. Activity completes;
9. held capacity is released.

### Sad path — blocked dependency

Activity remains blocked_dependency until the predecessor Activity is durably completed.

### Sad path — material shortage

Activity enters waiting_material and does not request scarce execution capacity until material
identity and quantity are both feasible.

### Sad path — failed inspection / rework

A failed ConstructionInspection remains failed. Activity enters rework, reacquires resources,
executes rework and creates a new ConstructionInspection occurrence.

### Sad path — disruption

Weather or procurement scenarios alter prerequisite context. Scenarios never directly assign
Activity or Inspection state.

## 7. Invariants

CONST-01 — Dependency gate: release from a blocked dependency requires durable predecessor completion.

CONST-02 — Material before scarce capacity: material feasibility is checked before crew/equipment acquisition.

CONST-03 — Capacity before execution: executing requires durable crew/equipment ownership.

CONST-04 — Inspection identity: failed inspection remains terminal evidence after rework.

CONST-05 — Acceptance gate: Activity accepts only after a correlated Inspection has passed.

CONST-06 — Measurement gate: Activity completes only with a positive correlated durable ConstructionMeasurement.

CONST-07 — Rework reacquisition: rework cannot resume execution without reacquiring required capacity.

CONST-08 — Scenario discipline: scenarios alter prerequisites/context, never business state directly.

CONST-09 — Restart equivalence: continuous/rebuilt runs converge at dependency, material, resource,
inspection-failure/rework and measured-before-complete boundaries.

## 8. Restart semantics

Reference-grade gates cover:

1. blocked dependency;
2. waiting material / partially committed material staging;
3. durable planned-start ScheduledWork before resource demand;
4. resource demand pending and outage cleanup;
5. execution capacity held with post-finish cleanup;
6. failed inspection before rework;
7. rework waiting for capacity;
8. inspector cleanup after accept/reject;
9. measured with completion still pending.

## 9. Executable evidence

| Specification area | Current implementation/evidence | Status |
|---|---|---|
| Activity entity | entities.py | implemented |
| Inspection entity | entities.py | implemented |
| Measurement entity | entities.py | implemented |
| Activity StateChart | statecharts.py + topology test | implemented |
| Inspection StateChart | statecharts.py + topology test | implemented |
| direct probabilistic gating | TransitionPolicy test | implemented |
| construction scenarios | scenarios.py | implemented |
| dependency execution | predecessor measurement/completion gate + tests | implemented |
| material identity / quantity | Store + Container staging + crash-recovery tests | implemented |
| crew / equipment / inspector resources | durable Resource reconcilers + contention/outage cleanup tests | implemented |
| inspection / rework execution | immutable inspection occurrences + reinspection tests | implemented |
| measurement completion gate | ConstructionMeasurement evidence + predecessor/activity tests | implemented |
| finite scenario recovery | procurement/weather disruption tests | implemented |
| restart equivalence | planned-start and failed-inspection/rework restart tests | implemented |

## Promotion decision

Current status: **Reference implementation**.

Promotion is based on executable evidence for predecessor and measurement gates, Store+Container
material staging, durable planned-start scheduling, crew/equipment/inspector ownership,
post-commit cleanup recovery, immutable inspection/reinspection identity, finite disruption
recovery, and restart equivalence across planned-start and failed-inspection/rework boundaries.
