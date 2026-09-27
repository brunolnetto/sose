# Insurance Reference Domain Specification

## 1. Purpose and scope

This example models an insurance claim without collapsing coverage, assessment,
fraud review, reserve, and payment into one lifecycle.

Durable entities:

- `Policy`;
- `Claim`;
- `DocumentRequest`;
- `Assessment`;
- `FraudInvestigation`;
- `Reserve`;
- `Payment`.

## 2. Operational story

A covered Claim requests documentation. Documentation has a durable deadline.
Satisfied documentation allows the Claim into a severity-prioritized assessment
queue. An adjuster assesses the Claim and may approve, reject, or flag it for
fraud review.

Approved claims require a separately durable Reserve before any Payment can be
created. A durable payment date makes the Payment due, but does not perform the
payout: payment-processor capacity is still required.

Payments may be partial before becoming fully paid. Rejected claims may be
reopened for new documentation and a fresh assessment round.

## 3. StateCharts

Claim:

    opened
      -> pending_documents
      -> ready_for_assessment
      -> assessing
         -> approved
         -> rejected -> reopened -> pending_documents
         -> fraud_review -> ready_for_assessment
      -> payment_scheduled
      -> paid

Payment:

    planned -> scheduled -> due -> paid
                           -> partially_paid -> paid
                           -> failed -> due

Fraud investigation:

    opened -> assigned -> cleared
                      -> confirmed

All transitions are orchestration-gated.

## 4. Durable ownership

Durable semantic truth includes:

- entity lifecycle state;
- immutable DomainEvent history;
- Command and ScheduledWork for document deadlines and payment dates;
- PriorityStore claim-queue evidence;
- ResourceDemand / ResourceReservation / ResourceReleaseIntent for adjusters,
  fraud investigators, and payment processors;
- deterministic IDs for document requests, assessments, investigations,
  reserves, and payments;
- correlation metadata across the complete claim flow;
- ScenarioRuntimeState and SimulationPosition.

Backend-native SimPy events, callbacks, queues, generators, and handles are
ephemeral and reconstructed from durable intent.

## 5. Resources and queueing

Resources:

- `claims_adjuster`;
- `fraud_investigator`;
- `payment_processor`.

Claims enter `claim_queue` with severity priority. A post-fraud reassessment
uses a new queue occurrence and a new Assessment identity.

## 6. Happy path

1. active Policy provides coverage;
2. Claim requests documents;
3. documents are satisfied before the durable deadline;
4. Claim enters severity-prioritized queue;
5. adjuster capacity is acquired;
6. Assessment approves;
7. Reserve is established;
8. Payment is created and durably scheduled;
9. payment date makes Payment due;
10. payment processor executes payout;
11. Claim becomes paid;
12. Reserve is released.

## 7. Sad paths

### Missing documents

DocumentRequest expires through ScheduledWork. The Claim remains
pending_documents and cannot proceed using the expired request.

### Fraud confirmed

Assessment preserves fraud-flagged evidence. FraudInvestigation confirms the
case and Claim becomes rejected. No Reserve or Payment may be created.

### Fraud cleared

FraudInvestigation is preserved as cleared evidence. The Claim returns to
ready_for_assessment, re-enters the queue, reacquires adjuster capacity, and
uses a distinct Assessment occurrence.

### Partial payout

A due Payment may become partially_paid. Claim remains payment_scheduled and
Reserve remains established until the full payout completes.

### Reopen

Only rejected claims are reopened in this reference implementation. Reopening
requires fresh documentation and a new assessment cycle. Already-paid claims
are intentionally outside the supported reopen semantics.

## 8. Invariants

INS-01 — Coverage gate: claim processing requires Policy(active).

INS-02 — Document deadline: expired documentation cannot satisfy Claim readiness.

INS-03 — Queue before assessment: assessment work is selected through durable
claim-queue evidence and adjuster capacity.

INS-04 — Fraud evidence preservation: fraud review creates a distinct durable
FraudInvestigation; clearing fraud does not rewrite the prior Assessment.

INS-05 — Reserve before payment: Payment creation requires Claim(approved) and
Reserve(established).

INS-06 — Payment date is not payout authority: ScheduledWork may make Payment
due but payout still requires payment-processor capacity.

INS-07 — Partial payout: a partial Payment cannot mark Claim(paid) or release
the Reserve.

INS-08 — Reassessment identity: post-fraud reassessment uses a distinct
Assessment occurrence.

INS-09 — Scenario discipline: scenarios change prerequisite availability rather
than lifecycle state directly.

INS-10 — Restart equivalence: continuous and rebuilt runs converge across
document-deadline, adjuster-demand, and payment-date boundaries.

## 9. Scenarios

`catastrophe_capacity_scenario` temporarily removes adjuster availability.

`payment_processor_outage_scenario` temporarily removes payment availability.

Both are finite and require normal workflow reconciliation after recovery.

## 10. Executable evidence

| Specification area | Evidence | Status |
|---|---|---|
| durable entities | entities.py + tests | implemented |
| StateCharts | statecharts.py + topology tests | implemented |
| document deadline | ScheduledWork + expiry test | implemented |
| priority claim queue | PriorityStore runtime + tests | implemented |
| adjuster capacity | Resource runtime + scenario/restart tests | implemented |
| fraud investigation | fraud paths + tests | implemented |
| reserve-before-payment | invariant tests | implemented |
| payment date | ScheduledWork + restart test | implemented |
| partial payout | happy-path tests | implemented |
| rejected-claim reopen | exception test | implemented |
| finite scenarios | scenario tests | implemented |
| restart equivalence | deadline/resource/payment tests | implemented |

## Promotion decision

Current status: **Reference implementation**.

Promotion is based on executable evidence for coverage/documentation gating, severity-prioritized queueing, assessment/fraud/reassessment identity, reserve-before-payment, durable payment dates, partial payout, rejected-claim reopening, finite scenarios, post-commit crash reconciliation, and restart equivalence across ScheduledWork and ResourceDemand boundaries.
