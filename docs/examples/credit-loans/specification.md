# Credit & Loans Reference Domain Specification

## 1. Purpose

Credit & Loans exercises durable recurring financial obligations rather than a
single linear transaction.

Durable entities:

- `LoanApplication`
- `CreditDecision`
- `Loan`
- `Installment`
- `Payment`
- `DelinquencyCase`
- `CollectionCase`
- `Restructure`

## 2. Canonical happy path

1. LoanApplication is submitted.
2. Credit analyst capacity is acquired.
3. Application enters underwriting.
4. CreditDecision is approved.
5. LoanApplication is approved.
6. Loan is created only from those two durable facts.
7. Loan is disbursed and enters servicing.
8. Multiple Installments are created with independent ScheduledWork due dates.
9. Each due Installment is paid through immutable Payment occurrences.
10. Partial payments update the obligation without completing it.
11. Final repayment drives Loan outstanding balance to zero.
12. Loan becomes paid_off.

## 3. Delinquency path

1. Installment becomes due.
2. A separate durable grace schedule may fire `miss`.
3. Installment becomes overdue.
4. DelinquencyCase is created deterministically.
5. Loan becomes delinquent.
6. CollectionCase is created and collection-agent capacity is acquired.
7. Contact may produce a promise-to-pay and durable follow-up schedule.
8. Follow-up may escalate the CollectionCase.
9. Escalated collection may default Loan and DelinquencyCase.

## 4. Restructure path

A delinquent Loan may create a durable Restructure occurrence.

Applying it:

- moves Loan through restructuring;
- marks affected active obligations `restructured`;
- cancels their stale overdue schedules;
- preserves DelinquencyCase and CollectionCase history;
- creates a replacement Installment for remaining outstanding balance;
- schedules the replacement obligation;
- resumes Loan servicing.

Old obligations are never rewritten as paid.

## 5. Durable ledger semantics

Payment posting and balance application are separate crash boundaries.

A committed `Payment(posted)` is authoritative even when a crash occurs before
its amount is projected onto Installment/Loan balances.

Each Installment and Loan stores `applied_payment_ids`. Reconciliation checks
that durable set before updating balances, making replay idempotent and
preventing double application.

## 6. Scheduling semantics

`DurableScheduler` owns:

- recurring installment due work;
- overdue grace work;
- collection promise follow-up work.

Domain logic still decides whether those schedules are legal.

Cancellation uses the shared ScheduledWork lifecycle API.

## 7. Resource semantics

Resources:

- `credit_analyst`
- `collection_agent`

The shared durable resource lifecycle API handles request/grant/withdrawal.
Underwriting and collections remain domain-owned eligibility decisions.

## 8. Probabilistic intentions

Application approval/rejection and installment miss are eligible probabilistic
intentions.

Ledger transitions such as payment posting, partial application, completion and
restructure remain orchestration-gated and cannot be sampled directly.

## 9. Scenario semantics

`macroeconomic_stress_scenario` temporarily removes underwriting and collection
availability and exposes `credit_loans.stress.active=True`.

The scenario never mutates business entity states directly.

## 10. Invariants

CL-01 — Loan requires both LoanApplication(approved) and CreditDecision(approved).

CL-02 — Each installment is an independent durable obligation with its own due time.

CL-03 — Payment is immutable occurrence evidence, not a mutable amount field.

CL-04 — One Payment ID may affect balances at most once.

CL-05 — Partial repayment cannot mark an obligation paid.

CL-06 — Delinquency requires an overdue Installment.

CL-07 — Collection history is independent of Loan state.

CL-08 — Default requires active delinquency collection and escalation evidence.

CL-09 — Restructure preserves prior delinquency/collection history.

CL-10 — Restructure supersedes old obligations and creates replacement obligation.

CL-11 — Scenario effects alter prerequisites, not business states directly.

CL-12 — Restart equivalence holds across due schedules, ledger application,
delinquency creation, and collection follow-up.

## 11. Crash/restart boundaries

Executable evidence covers:

1. pending installment due ScheduledWork;
2. Payment(posted) before balance projection;
3. repeated payment reconciliation without double-apply;
4. Installment(overdue) before DelinquencyCase creation;
5. promised CollectionCase with follow-up ScheduledWork pending.

## 12. Executable evidence

| Area | Evidence | Status |
|---|---|---|
| application/decision topology | statechart tests | implemented |
| stochastic approve/reject intention | policy test | implemented |
| recurring installments | happy-path test | implemented |
| partial repayment | happy-path test | implemented |
| payment idempotence | restart test | implemented |
| delinquency creation | delinquency/restart tests | implemented |
| collection follow-up | delinquency/restart tests | implemented |
| default | delinquency test | implemented |
| restructure | restructure test | implemented |
| macro stress | scenario test | implemented |
| restart toolkit integration | restart-equivalence tests | implemented |

## Promotion decision

Current status: **Partial**.

Promotion awaits CI and final crash-boundary audit.
