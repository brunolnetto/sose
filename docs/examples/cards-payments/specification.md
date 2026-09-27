# Cards & Payments Reference Domain Specification

## 1. Purpose and scope

This example models a card payment from authorization through capture and settlement,
with explicit pre-capture reversal, post-settlement refund, and a separate dispute lifecycle.

The domain exists to exercise transactional causality, irreversible boundaries, retries,
reversals, post-settlement exception handling, and durable audit history.

Initial scope:

- Payment durable entity;
- Dispute durable entity;
- authorization / decline decision;
- capture and settlement;
- authorization reversal before capture;
- refund only after settlement;
- dispute evidence, chargeback, and resolution;
- processor outage and fraud-pressure scenarios.

## 2. Operational story

A Payment begins in authorization_requested. The authorization decision may authorize or
decline. An authorized payment may be reversed before capture. Once captured, that
authorization reversal path is closed. Captured value may settle, and only a settled
payment may be refunded.

A dispute is not a mutation that erases settlement history. It is a separate correlated
durable entity representing a post-settlement case. Evidence, chargeback, and resolution
belong to Dispute while the Payment retains its own transaction history.

## 3. Domain entities

### 3.1 Payment

States: authorization_requested, authorized, captured, settled, declined, reversed, refunded.

Owns the transaction lifecycle and the semantic boundary between reversal and refund.

### 3.2 Dispute

States: opened, evidence_requested, under_review, chargeback, merchant_won, cardholder_won, withdrawn.

Owns post-settlement contest and chargeback resolution. It does not rewrite historical
Payment settlement facts.

## 4. Persistent data model / ERD

The model is semantic rather than a physical SQL schema.

### 4.1 Business entities ERD

    Payment 1 ---- 0..N Dispute
            process correlation

The relationship is represented through stable process identity/correlation rather than
a required entity foreign-key field.

### 4.2 Durable operational ERD

Target reference-grade durable relationships:

    Payment / Dispute
        -> Command -> ScheduledWork
        -> DomainEvent

    ResourceDefinition
        -> ResourceDemand -> ResourceReservation -> ResourceReleaseIntent

    ScenarioRuntimeState
    SimulationPosition

Planned resources include authorization_processor, settlement_processor, and dispute_analyst.
Scheduled work will represent settlement windows, retry backoff, evidence deadlines,
and dispute resolution deadlines.

### 4.3 Persistence ownership

| Business fact | Durable owner |
|---|---|
| payment lifecycle | Payment.state |
| dispute lifecycle | Dispute.state |
| future settlement/retry/deadline work | Command + ScheduledWork |
| processor / analyst demand | ResourceDemand |
| held processing capacity | ResourceReservation |
| interrupted release | ResourceReleaseIntent |
| immutable transaction/case history | DomainEvent |
| scenario intervention | ScenarioRuntimeState |
| logical recovery boundary | SimulationPosition |

## 5. StateCharts

### 5.1 Payment StateChart

    authorization_requested
      -> authorized -> captured -> settled -> refunded
      -> declined

    authorized -> reversed

Reversal is intentionally legal only from authorized. Refund is intentionally legal
only from settled. Capture/settle/refund/reverse remain explicit orchestrated events.

### 5.2 Dispute StateChart

    opened
    -> evidence_requested
    -> under_review
    -> chargeback
       -> merchant_won
       -> cardholder_won

Withdrawal is legal before chargeback.

## 6. Process specifications

### 6.1 Happy path

    Payment(authorization_requested)
    -> authorization capacity / decision
    -> authorized
    -> capture
    -> durable settlement schedule
    -> settlement capacity
    -> settled

### 6.2 Sad path — authorization decline

Decline is a terminal payment outcome. No capture or settlement work may be created.

### 6.3 Sad path — authorization reversal

    authorized -> reverse -> reversed

The reversal happens before capture and terminates the payment lifecycle. It is not a
refund and must not create settlement work.

### 6.4 Sad path — settlement retry

A captured payment may wait for processor availability or retry after a transient
settlement failure. Retry timing must be durable ScheduledWork; backend callbacks are
reconstructible mechanics only.

### 6.5 Sad path — refund

Refund is only legal after settlement. Historical capture/settlement events remain
immutable; refund is a new terminal business fact rather than deletion of settlement.

### 6.6 Sad path — dispute / chargeback

A dispute is created under the same payment correlation after settlement. Evidence and
review progress independently. Chargeback outcome belongs to Dispute and does not
pretend that the original Payment never settled.

## 7. Commands and domain events

Payment commands: authorize, decline, capture, reverse, settle, refund.

Dispute commands: request_evidence, submit_evidence, issue_chargeback,
resolve_merchant, resolve_cardholder, withdraw.

Only authorize and decline are direct stochastic outcomes in the initial policy.

## 8. Invariants

PAY-01 — Authorization gate: capture requires Payment(authorized).

PAY-02 — Reversal boundary: reverse is legal before capture only.

PAY-03 — Settlement boundary: settle requires captured value.

PAY-04 — Refund boundary: refund requires settled value and preserves settlement history.

PAY-05 — Dispute independence: dispute/chargeback lifecycle does not overwrite Payment history.

PAY-06 — Processor gating: operational capacity must exist before processor-gated lifecycle claims.

PAY-07 — Durable retry: retry/backoff intent survives restart as ScheduledWork.

PAY-08 — Scenario discipline: processor/fraud scenarios alter context, not entity state directly.

PAY-09 — Restart equivalence: continuous and rebuilt executions converge across
authorization wait, captured-before-settlement, retry wait, refund, and dispute boundaries.

## 9. Durable truth and ownership

Durable semantic truth includes entities, immutable events, commands/schedules,
processor/analyst resource truth, scenario state, and logical recovery position.

Backend network calls, callbacks, worker tasks, SimPy objects, queues, and generators
remain ephemeral and reconstructible.

## 10. Restart semantics

Reference-grade restart gates will cover:

1. authorization request pending capacity;
2. authorized before capture;
3. captured with settlement scheduled;
4. settlement retry scheduled;
5. settled before refund;
6. dispute evidence pending;
7. chargeback awaiting resolution.

## 11. Scenario specification

Processor outage temporarily makes payment-processing capacity unavailable. Fraud pressure
changes authorization-risk context. Neither scenario assigns Payment or Dispute state directly.

## 12. Example runs

Nominal:

    authorization_requested -> authorized -> captured -> settled

Reversal:

    authorization_requested -> authorized -> reversed

Refund:

    authorization_requested -> authorized -> captured -> settled -> refunded

Dispute:

    Payment(settled) + Dispute(opened) -> evidence_requested -> under_review
    -> chargeback -> merchant_won/cardholder_won

## 13. Executable evidence

| Specification area | Current implementation/evidence | Status |
|---|---|---|
| Payment entity | entities.py | implemented |
| Dispute entity | entities.py | implemented |
| Payment StateChart | statecharts.py + topology test | implemented |
| Dispute StateChart | statecharts.py + topology test | implemented |
| stochastic authorization decision | TransitionPolicy + test | implemented |
| payment scenarios | scenarios.py | implemented |
| durable processor resources | — | missing |
| durable settlement scheduling | — | missing |
| retry/backoff execution | — | missing |
| refund end-to-end path | — | missing |
| dispute/chargeback end-to-end path | — | missing |
| restart equivalence | — | missing |

## Promotion decision

Current status: **Partial**.

The transaction and dispute contracts are executable, but reference promotion requires
durable processor capacity, settlement/retry schedules, complete happy/sad flows,
scenario execution, and restart-equivalence evidence.
