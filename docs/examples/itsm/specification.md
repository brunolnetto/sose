# IT Service Management Reference Domain Specification

## 1. Purpose and scope

This example models an operational support incident from opening through triage, assignment,
active work, resolution and closure, with SLA-driven escalation and reopen behavior.

The domain exists to exercise durable case queues, severity ordering, human capacity, timers,
escalation ownership, and recovery from pending SLA deadlines.

Initial scope:

- Incident durable entity;
- Escalation durable entity;
- triage, assignment and work lifecycle;
- explicit escalation case;
- resolution, close and reopen;
- incident-storm and staff-shortage scenarios.

## 2. Operational story

An Incident begins opened and must be triaged before assignment. Assignment requires durable
support capacity. Active work can resolve normally or be escalated when an SLA boundary is
reached. Escalation is represented by a separate durable Escalation entity so acknowledgement,
ownership, mitigation and completion remain explicit audit facts.

A resolved Incident may close or reopen. Reopen returns the same Incident to in_progress because
it is continuation of the same support case, while escalation occurrences remain separately
durable.

## 3. Domain entities

### 3.1 Incident

States: opened, triaged, assigned, in_progress, escalated, resolved, closed.

Owns the main service lifecycle. It does not own backend queues, analyst request handles,
callbacks, or SLA timer objects.

### 3.2 Escalation

States: raised, acknowledged, owned, mitigated, completed, cancelled.

Represents a specific escalation occurrence correlated to an Incident. Its lifecycle is not
collapsed into mutable escalation flags on Incident.

## 4. Persistent data model / ERD

The model is semantic rather than a physical SQL schema.

### 4.1 Business entities ERD

    Incident 1 ---- 0..N Escalation
             process correlation

The relationship is stable process correlation, not a required entity foreign key.

### 4.2 Durable operational ERD

Target durable relationships:

    Incident / Escalation
        -> Command -> ScheduledWork
        -> DomainEvent

    ResourceDefinition
        -> ResourceDemand -> ResourceReservation -> ResourceReleaseIntent

    StoreDefinition
        -> DurableStoreItem
        -> StorePutIntent
        -> StoreGetRequest -> StoreGetResult

    ScenarioRuntimeState
    SimulationPosition

Planned resources: support_agent and escalation_manager.
Planned Store: incident_queue as priority ordering by severity.
Scheduled work: response SLA and escalation acknowledgement/deadline work.

### 4.3 Persistence ownership

| Business fact | Durable owner |
|---|---|
| incident lifecycle | Incident.state |
| escalation lifecycle | Escalation.state |
| severity queue membership/order | Store durable records |
| agent/manager demand | ResourceDemand |
| held human capacity | ResourceReservation |
| interrupted release | ResourceReleaseIntent |
| SLA/deadline intent | Command + ScheduledWork |
| audit history | DomainEvent |
| scenario intervention | ScenarioRuntimeState |
| logical recovery boundary | SimulationPosition |

## 5. StateCharts

### 5.1 Incident StateChart

    opened -> triaged -> assigned -> in_progress -> resolved -> closed
                  \          \
                   -> escalated -> resolved

Resolved incidents may reopen to in_progress.

### 5.2 Escalation StateChart

    raised -> acknowledged -> owned -> mitigated -> completed

Cancellation is legal before mitigation is committed.

All Incident and Escalation transitions are explicit orchestration events; neither chart is
directly probabilistic.

## 6. Process specifications

### 6.1 Happy path

    Incident(opened)
    -> triage
    -> durable priority queue
    -> acquire support agent
    -> assigned
    -> in_progress
    -> resolve
    -> resolved
    -> close
    -> closed

### 6.2 Sad path — SLA escalation

    Incident(triaged|assigned|in_progress)
    -> durable SLA deadline
    -> create Escalation(raised)
    -> Incident(escalated)
    -> manager acknowledgement / ownership
    -> mitigation
    -> Incident(resolved)

Escalation history remains durable after Incident resolution.

### 6.3 Sad path — support contention

Incidents remain durably queued by severity while support capacity is occupied. Queue order
must survive restart; backend queue identity is not semantic truth.

### 6.4 Sad path — reopen

A resolved Incident may reopen to in_progress. Historical resolution events remain immutable;
reopen is a new business fact rather than deletion of resolution.

### 6.5 Sad path — staff shortage scenario

Scenario state makes support prerequisites unavailable. It does not directly write Incident
state. Orchestration leaves cases queued/pending until capacity context recovers.

## 7. Commands and domain events

Incident commands: triage, assign, start, escalate, resolve, close, reopen.

Escalation commands: acknowledge, take_ownership, mitigate, complete, cancel.

Successful transitions emit immutable state-transition events under stable incident correlation.

## 8. Invariants

ITSM-01 — Triage before assignment: Incident cannot assign directly from opened.

ITSM-02 — Capacity before ownership: assignment requires durable support capacity.

ITSM-03 — Durable severity queue: pending cases survive restart with stable ordering.

ITSM-04 — Explicit SLA intent: escalation deadlines are ScheduledWork, not backend timers.

ITSM-05 — Escalation identity: each escalation occurrence is independently durable.

ITSM-06 — Resolution history: reopen does not erase prior resolution.

ITSM-07 — Scenario discipline: scenarios alter capacity/context and never assign lifecycle state.

ITSM-08 — Restart equivalence: continuous and rebuilt executions converge across queued,
assigned, SLA-pending, escalated, resolved and reopened boundaries.

## 9. Durable truth and ownership

Durable truth consists of Incident/Escalation entities, commands/events/schedules, priority
Store records, resource demand/ownership, scenario state, and logical recovery position.

Backend worker queues, task handles, callbacks, timers, SimPy objects and Python generators are
ephemeral and reconstructible.

## 10. Restart semantics

Reference-grade restart gates will cover:

1. triaged and queued;
2. support-agent demand pending;
3. assigned before start;
4. active incident with SLA deadline;
5. escalation raised/acknowledged;
6. resolved before close;
7. reopened in progress.

## 11. Scenario specification

Incident storm increases arrival pressure context. Staff shortage temporarily makes support
capacity unavailable. Neither scenario mutates Incident/Escalation lifecycle directly.

## 12. Example runs

Nominal:

    opened -> triaged -> assigned -> in_progress -> resolved -> closed

Escalated:

    triaged/assigned/in_progress -> escalated
    + Escalation(raised -> acknowledged -> owned -> mitigated -> completed)
    -> Incident(resolved)

Reopen:

    resolved -> reopen -> in_progress -> resolve -> closed

## 13. Executable evidence

| Specification area | Current implementation/evidence | Status |
|---|---|---|
| Incident entity | entities.py | implemented |
| Escalation entity | entities.py | implemented |
| Incident StateChart | statecharts.py + topology test | implemented |
| Escalation StateChart | statecharts.py + topology test | implemented |
| direct probabilistic gating | TransitionPolicy tests | implemented |
| ITSM scenarios | scenarios.py | implemented |
| durable priority incident queue | PriorityStore + severity-order test | implemented |
| support / escalation resources | durable Resource reconcilers + tests | implemented |
| SLA / escalation scheduling | durable `ScheduledWork` + escalation flow | implemented |
| happy path | `run_happy_path` + test | implemented |
| contention / escalation sad paths | priority queue + SLA escalation tests | implemented |
| reopen execution | resolution/reopen regression test | implemented |
| restart equivalence | pending-SLA restart-equivalence test | implemented |

## Promotion decision

Current status: **Reference implementation**.

Promotion is based on executable evidence for durable severity ordering, support ownership,
SLA-triggered escalation, independent Escalation identity, finite staff-shortage recovery,
resolve/reopen history, and restart equivalence across a pending SLA.
