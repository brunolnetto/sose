# Record-to-Report Reference Domain Specification

## 1. Purpose and scope

This example models Record-to-Report as a durable accounting-close workflow rather than one monolithic period state machine.

Entities:

- `JournalEntry`: posting outcome;
- `ReconciliationItem`: matching / exception truth;
- `Adjustment`: corrective accounting evidence;
- `CloseTask`: time-dependent close work;
- `AccountingPeriod`: period-level close/reopen state.

The reference proves that the accounting calendar may trigger close work, but cannot itself assert that a period is ready to close.

## 2. Operational story

A JournalEntry is submitted and posted. Its correlated ReconciliationItem is matched or marked unmatched. Unmatched items may require an Adjustment; a posted Adjustment reconciles the item.

A durable ScheduledWork starts the CloseTask at the accounting-calendar deadline. The CloseTask may complete only when posted journal and reconciliation evidence satisfy the close prerequisites and a close accountant owns execution capacity.

A closed AccountingPeriod may be explicitly reopened. Reopening creates a new deterministic CloseTask occurrence; historical close evidence is not overwritten.

## 3. StateCharts

JournalEntry:

    drafted -> submitted -> posted
                         -> rejected

ReconciliationItem:

    pending -> reconciling -> matched
                          -> unmatched
                             -> adjustment_required
                                -> reconciled
                             -> rejected

Adjustment:

    proposed -> submitted -> approved -> posted
                         \-> rejected

CloseTask:

    pending -> in_progress -> completed
                          \-> failed -> in_progress

AccountingPeriod:

    open -> close_ready -> closed -> reopened -> close_ready -> closed

All transitions are orchestration-gated.

## 4. Persistent model and ownership

Durable semantic truth includes:

- entity lifecycle state;
- immutable DomainEvent history;
- Command and ScheduledWork for close deadlines;
- ResourceDemand / ResourceReservation / ResourceReleaseIntent for posting, reconciliation, and close capacity;
- deterministic correlation across period, journal, reconciliation, adjustment, and close task;
- ScenarioRuntimeState;
- SimulationPosition.

Backend-native queues, callbacks, SimPy events, generators, and resource handles are ephemeral and reconstructible.

## 5. Resources and calendars

Resources:

- `posting_processor`;
- `reconciliation_analyst`;
- `close_accountant`.

The accounting calendar is represented by ScheduledWork targeting a CloseTask. The deadline starts close work; it does not bypass accounting prerequisites.

## 6. Happy path

1. journal is submitted and posted;
2. reconciliation starts and matches;
3. close task is durably scheduled;
4. close deadline fires;
5. close-accountant capacity is acquired;
6. period moves open → close_ready → closed;
7. close task completes;
8. capacity is released.

## 7. Representative sad paths

### Rejected posting

A submitted JournalEntry may terminate as rejected and cannot satisfy close prerequisites.

### Unmatched reconciliation / adjustment

An unmatched item becomes adjustment_required. Adjustment identity is deterministic. Posting the adjustment transitions the item to reconciled.

### Posting outage

Posting scenario unavailability prevents processor demand from leaking and recovers when the finite scenario expires.

### Close-team shortage

A CloseTask may already be in_progress while close-team capacity is unavailable. The AccountingPeriod remains open until the prerequisite context recovers.

### Controlled reopening

Only a closed AccountingPeriod can reopen. Reopening creates a new CloseTask occurrence and preserves prior close history.

## 8. Invariants

R2R-01 — Posting before reconciliation: reconciliation requires durable JournalEntry(posted).

R2R-02 — Adjustment causality: Adjustment creation requires ReconciliationItem(adjustment_required).

R2R-03 — Close deadline is not close authority: ScheduledWork may start CloseTask but cannot close AccountingPeriod directly.

R2R-04 — Close prerequisites: period close requires posted journal and matched/reconciled item.

R2R-05 — Capacity before close: close transition requires durable close-accountant ownership.

R2R-06 — Audit preservation: failed/unmatched/rejected outcomes remain durable history and are never silently rewritten.

R2R-07 — Controlled reopen: only AccountingPeriod(closed) may reopen; reclose uses a distinct deterministic CloseTask.

R2R-08 — Scenario discipline: scenarios alter prerequisite availability, not entity lifecycle state directly.

R2R-09 — Restart equivalence: continuous and rebuilt runs converge across pending close schedule, pending close-accountant demand, and unmatched-before-adjustment boundaries.

## 9. Restart semantics

Executable recovery boundaries:

1. close ScheduledWork pending before deadline;
2. CloseTask in_progress with close-accountant demand queued;
3. ReconciliationItem(unmatched) before Adjustment creation;
4. controlled reopen with fresh close task.

## 10. Scenario specification

`posting_outage_scenario` temporarily disables posting availability.

`close_team_shortage_scenario` temporarily disables close-team availability.

Neither scenario mutates business state directly.

## 11. Executable evidence

| Specification area | Evidence | Status |
|---|---|---|
| durable entities | entities.py + tests | implemented |
| StateCharts | statecharts.py + topology tests | implemented |
| explicit commands/events | simulation.py | implemented |
| deterministic identity/correlation | simulation.py | implemented |
| durable close calendar | ScheduledWork close test | implemented |
| happy path | run_happy_path + tests | implemented |
| unmatched/adjustment path | run_adjustment_path + tests | implemented |
| resource capacity | posting/reconciliation/close Resources | implemented |
| legal/illegal prerequisites | invariant tests | implemented |
| posting/close scenarios | scenario tests | implemented |
| restart: close schedule | restart-equivalence test | implemented |
| restart: close capacity | ResourceDemand rebuild test | implemented |
| restart: missing adjustment | causal recovery test | implemented |
| reopen / reclose | reopen test | implemented |

## Promotion decision

Current status: **Partial**.

The implementation meets the reference evidence target in code and tests; status promotion is intentionally deferred until CI and review validate the branch.
