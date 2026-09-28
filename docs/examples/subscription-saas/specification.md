# Subscription / SaaS — Executable Specification

## 1. Purpose

Exercise future-effective commercial intent while keeping Subscription,
Entitlement, ChangeRequest, ScheduledWork, and immutable occurrence evidence
separate.

## 2. Durable entities

Subscription lifecycle:
active -> cancellation_pending -> ended, with cancellation_pending -> active for
withdrawal.

Entitlement lifecycle:
active -> superseded or active -> revoked.

ChangeRequest lifecycle:
scheduled -> applied or scheduled -> cancelled.

SubscriptionOccurrence lifecycle:
captured -> committed.

## 3. Plan-change semantics

Only an active Subscription may request a plan change. The effective timestamp
must be in the future and strictly inside the current term. Only one scheduled
ChangeRequest may own a future amendment boundary at a time.

At the effective boundary, ChangeRequest becomes applied. Reconciliation then
supersedes the prior Entitlement, creates/reuses the deterministic replacement
Entitlement, updates Subscription projection, and commits immutable plan-change
evidence.

If restart occurs after the ChangeRequest is applied but before reconciliation,
the same deterministic Entitlement and occurrence are reused.

## 4. Cancellation-at-period-end

Requesting cancellation:

1. cancels any still-scheduled plan amendments;
2. moves Subscription to cancellation_pending;
3. schedules Subscription end at term end;
4. schedules active Entitlement revocation at the same boundary.

Withdrawal removes both boundaries and returns the commercial Subscription to
active.

## 5. Invariants

SUB-01 — Subscription and Entitlement are different durable truths.

SUB-02 — Future-effective intent is a durable ChangeRequest, not an anonymous
timer.

SUB-03 — At most one scheduled plan change owns an amendment boundary.

SUB-04 — An applied ChangeRequest reconciles idempotently after restart.

SUB-05 — Entitlement replacement preserves the superseded historical entity.

SUB-06 — Plan-change occurrence identity prevents duplicate historical evidence.

SUB-07 — Cancellation-at-period-end removes pending amendment ownership before
the end boundary becomes authoritative.

SUB-08 — Cancellation withdrawal removes end/revoke ScheduledWork.

SUB-09 — No billing/proration arithmetic is inferred by the lifecycle model.

## 6. Happy path

Schedule Basic -> Pro inside the current term, execute the boundary, reconcile,
and verify one superseded Basic entitlement, one active Pro entitlement, and one
committed immutable occurrence.

## 7. Representative sad paths

- second concurrent future plan change;
- effective date outside the active term;
- new amendment after cancellation request;
- cancellation while amendment is pending;
- cancellation withdrawal.

## 8. Restart equivalence

- restart after ChangeRequest applied but before entitlement reconciliation;
- restart while cancellation-at-period-end boundaries are pending.

## 9. Executable evidence

| Requirement | Evidence |
| --- | --- |
| statecharts | test_subscription_saas_statecharts.py |
| happy path | test_subscription_saas_happy_path.py |
| sad paths | test_subscription_saas_sad_paths.py |
| restart equivalence | test_subscription_saas_restart_equivalence.py |
| scheduled work | sad/restart suites |
| immutable occurrence | happy/restart suites |

## 10. Promotion decision

Current status: **Reference implementation**.

This closes the previously deferred Subscription frontier without reopening
Money. Pricing/proration remain outside scope until independent evidence demands
a shared arithmetic contract.
