# Hospitals Reference Domain Specification

## 1. Purpose and scope

This example models an Admission from arrival through triage, bed wait/allocation, treatment,
possible ICU escalation, and discharge or transfer. A separate TreatmentEpisode models
capacity-constrained procedures that may be interrupted when clinical policy permits emergency
prioritization.

The domain exists to exercise PriorityStore triage ordering, scarce non-preemptive bed/team
capacity, explicit transfer/deterioration branches, preemptive procedure capacity, and recovery
from committed preemption.

Initial scope:

- durable Admission and TreatmentEpisode entities;
- triage and bed allocation lifecycle;
- ward and ICU capacity;
- treatment / deterioration / discharge / transfer;
- explicit treatment interrupt/resume;
- occupancy-surge and emergency-procedure scenarios.

## 2. Operational story

An Admission is triaged before entering a bed wait. Ward and ICU beds are scarce but are not
preempted simply because a higher-acuity patient arrives. Deterioration releases or changes the
current capacity claim and routes the Admission toward ICU waiting.

A TreatmentEpisode owns one procedure occurrence. An elective episode may be interrupted only
through the preemptive procedure-suite mechanism. The durable ResourcePreemptionResult is the
evidence that justifies the `interrupt` transition. Resume requires the emergency reservation
to be released and normal procedure capacity to be reacquired.

## 3. Domain entities

### 3.1 Admission

States: admitted, triaged, waiting_bed, bed_allocated, treatment, waiting_icu, icu,
discharge_ready, discharged, transferred.

Owns patient-flow state. It does not own backend resource handles, queues or callbacks.

### 3.2 TreatmentEpisode

States: planned, waiting_capacity, in_progress, interrupted, completed, cancelled.

Owns one interruptible treatment/procedure occurrence. Historical preemption evidence remains
separate durable runtime truth.

## 4. Persistent data model / ERD

The relationships are semantic rather than physical database foreign keys.

### 4.1 Business entities ERD

    Admission 1 ---- 0..N TreatmentEpisode
              process correlation

### 4.2 Durable operational model

    Admission / TreatmentEpisode
        -> Command -> ScheduledWork
        -> DomainEvent

    StoreDefinition
        -> DurableStoreItem / StorePutIntent / StoreGetRequest / StoreGetResult

    ResourceDefinition
        -> ResourceDemand -> ResourceReservation -> ResourceReleaseIntent

    PreemptiveResourceDefinition
        -> PreemptiveResourceDemand
        -> PreemptiveResourceReservation
        -> PreemptiveResourceReleaseIntent
        -> ResourcePreemptionResult

    ScenarioRuntimeState
    SimulationPosition

Planned durable names:

- PriorityStore: triage_queue;
- Resources: ward_bed, icu_bed, clinical_team;
- PreemptiveResource: procedure_suite.

### 4.3 Persistence ownership

| Business fact | Durable owner |
|---|---|
| admission lifecycle | Admission.state |
| treatment lifecycle | TreatmentEpisode.state |
| triage order | Store durable records |
| bed/team demand | ResourceDemand |
| held bed/team capacity | ResourceReservation |
| procedure demand | PreemptiveResourceDemand |
| held procedure capacity | PreemptiveResourceReservation |
| committed interruption | ResourcePreemptionResult |
| interrupted release | release-intent records |
| scenario intervention | ScenarioRuntimeState |
| logical recovery boundary | SimulationPosition |

## 5. StateCharts

### 5.1 Admission

    admitted -> triaged -> waiting_bed -> bed_allocated -> treatment
                                             |             |
                                             +--> waiting_icu -> icu

    treatment/icu -> discharge_ready -> discharged
    waiting_bed/waiting_icu -> transferred

### 5.2 TreatmentEpisode

    planned -> waiting_capacity -> in_progress -> completed
                                   |
                                   -> interrupted -> in_progress

Cancellation is legal before completion from planned/waiting/interrupted states.

Neither chart is directly probabilistic.

## 6. Process specifications

### 6.1 Happy path

    Admission(admitted)
    -> triage
    -> durable priority queue
    -> ward-bed + clinical-team capacity
    -> bed_allocated
    -> treatment
    -> discharge_ready
    -> discharged

### 6.2 Sad path — bed contention

Admission remains waiting_bed with durable queue/resource truth until capacity is available.

### 6.3 Sad path — deterioration and ICU

An Admission may deteriorate from bed_allocated/treatment into waiting_icu. ICU allocation
requires a durable ICU reservation; it is not inferred from queue removal.

### 6.4 Sad path — emergency procedure preemption

    elective TreatmentEpisode(in_progress)
    + normal procedure reservation
    -> emergency preemptive request
    -> ResourcePreemptionResult
    -> elective TreatmentEpisode(interrupted)
    -> emergency completes/releases
    -> elective normal request reacquired
    -> resume -> in_progress

The business interrupt must never precede durable preemption evidence.

### 6.5 Sad path — transfer

Transfer is explicit terminal Admission state from waiting_bed or waiting_icu. It is never
represented by silent removal from a wait queue.

## 7. Commands and domain events

Admission commands: triage, wait_bed, allocate_bed, start_treatment, deteriorate,
allocate_icu, ready_discharge, discharge, transfer.

TreatmentEpisode commands: queue, start, interrupt, resume, complete, cancel.

## 8. Invariants

HOSP-01 — Triage before capacity: bed demand follows triage.

HOSP-02 — Durable waiting: triage/bed waiting survives restart.

HOSP-03 — Capacity before claim: bed/team/procedure state transitions require durable capacity.

HOSP-04 — Beds are not emergency-preempted in this reference model.

HOSP-05 — Preemption evidence: TreatmentEpisode interrupt requires ResourcePreemptionResult.

HOSP-06 — Resume gating: interrupted treatment resumes only after normal capacity is reacquired.

HOSP-07 — Transfer truth: transferred is explicit durable terminal state.

HOSP-08 — Scenario discipline: scenarios alter availability/context, not business state directly.

HOSP-09 — Restart equivalence: continuous/rebuilt runs converge at bed wait, ICU wait,
procedure interruption, reacquisition and discharge boundaries.

## 9. Durable truth and ownership

Durable semantic truth consists of entities, immutable events, scheduled work, triage Store
records, normal/preemptive resource truth, preemption evidence, scenario state and recovery
position. Backend-native resource requests, queues, callbacks and generators remain ephemeral.

## 10. Restart semantics

Reference-grade gates will cover:

1. triaged and queued;
2. waiting for ward bed;
3. active treatment with held capacity;
4. waiting for ICU;
5. procedure preempted but business interrupt not yet reconciled;
6. interrupted procedure waiting to reacquire;
7. discharge-ready before discharge.

## 11. Scenario specification

Occupancy surge makes ward allocation unavailable for a finite period. Emergency-procedure
surge introduces emergency pressure on the procedure suite. Scenarios do not assign entity state.

## 12. Executable evidence

| Specification area | Current implementation/evidence | Status |
|---|---|---|
| Admission entity | entities.py | implemented |
| TreatmentEpisode entity | entities.py | implemented |
| Admission StateChart | statecharts.py + topology test | implemented |
| TreatmentEpisode StateChart | statecharts.py + topology test | implemented |
| direct probabilistic gating | TransitionPolicy test | implemented |
| hospital scenarios | scenarios.py | implemented |
| durable triage queue | — | missing |
| ward / ICU / clinical-team resources | — | missing |
| preemptive procedure execution | — | missing |
| preemption recovery | — | missing |
| happy / contention / transfer paths | — | missing |
| scenario recovery | — | missing |
| restart equivalence | — | missing |

## Promotion decision

Current status: **Partial**.

Reference promotion requires executable queue/resource/preemption/recovery behavior and restart
evidence for the stated boundaries.
