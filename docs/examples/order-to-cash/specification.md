# Order-to-Cash Reference Domain Specification

## 1. Purpose and scope

This example models the commercial-to-cash chain without collapsing it into one giant lifecycle.

The reference domain uses three durable entities:

- `SalesOrder` for credit and fulfillment progression;
- `Receivable` for the financial obligation after invoicing;
- `CollectionCase` for overdue recovery work.

The design goal is to prove cross-entity correlation and causality while preserving entity-local ownership.

## 2. Operational story

A SalesOrder is submitted, credit is either approved or held, fulfillment starts, may be partial, then completes, ships, and is invoiced.

Invoicing creates a correlated Receivable rather than extending the SalesOrder state machine into finance. The Receivable becomes due, may be collected, overdue, or disputed. Overdue recovery creates a separate CollectionCase occurrence. Collection work never rewrites the historical due/overdue facts.

## 3. Domain entities

### SalesOrder

States: submitted, credit_hold, ordered, fulfilling, partial_fulfillment, fulfilled, shipped, invoiced, cancelled.

### Receivable

States: open, due, overdue, disputed, collected.

### CollectionCase

States: opened, assigned, contacted, promised, escalated, resolved.

## 4. Durable operational model

Target reference-grade durable truth:

- SalesOrder, Receivable, CollectionCase entities;
- immutable DomainEvent history and stable correlation/causation;
- ScheduledWork for payment due dates, overdue boundaries, promise-to-pay follow-up, and collection escalation;
- ResourceDemand/Reservation/ReleaseIntent for fulfillment and collection capacity;
- Store/Container or correlated fulfillment evidence where inventory allocation is required;
- ScenarioRuntimeState;
- SimulationPosition.

Relationships are semantic rather than SQL foreign-key declarations.

## 5. StateCharts

Commercial flow:

    submitted
      -> ordered
      -> fulfilling
      -> partial_fulfillment
      -> fulfilled
      -> shipped
      -> invoiced

Credit exception:

    submitted -> credit_hold -> ordered

Financial flow:

    open -> due -> collected
             -> overdue -> collected
             -> disputed -> due

Collection flow:

    opened -> assigned -> contacted -> promised -> resolved
                              \-> escalated -> resolved

All transitions are orchestration-gated.

## 6. Process specifications

### Happy path

1. SalesOrder passes credit.
2. fulfillment capacity is acquired;
3. order is fully fulfilled and shipped;
4. invoice transition creates a correlated Receivable;
5. a durable due-date schedule fires;
6. Receivable becomes due and is collected.

### Sad path — credit hold

Credit unavailability holds the SalesOrder before fulfillment capacity is requested.

### Sad path — partial fulfillment

Partial fulfillment is explicit durable order state. Remaining work must continue from durable fulfillment evidence rather than silently overwriting quantities.

### Sad path — overdue and collection

A due Receivable crosses a durable overdue deadline, transitions to overdue, and creates a correlated CollectionCase. Assignment/contact/promise/escalation belong to that case.

### Sad path — dispute

A disputed Receivable preserves historical due/overdue events. Resolving the dispute returns the financial obligation to the appropriate due-processing flow rather than deleting history.

## 7. Invariants

O2C-01 — Credit before fulfillment: fulfillment capacity is not requested while SalesOrder is on credit hold.

O2C-02 — Fulfillment evidence before shipment: shipment requires durable fulfillment completion.

O2C-03 — Invoice causality: Receivable creation requires durable SalesOrder invoicing evidence and stable correlation.

O2C-04 — Durable due date: due/overdue transitions are driven by ScheduledWork, not backend timers.

O2C-05 — Collection identity: overdue recovery uses a separately durable CollectionCase.

O2C-06 — Collection does not rewrite finance history: collection resolution does not delete Receivable overdue/dispute events.

O2C-07 — Scenario discipline: credit/fulfillment scenarios change prerequisites and never assign lifecycle state directly.

O2C-08 — Restart equivalence: continuous and rebuilt executions converge across credit hold, partial fulfillment, invoice/receivable creation, due/overdue, and collection boundaries.

## 8. Restart semantics

Reference-grade recovery gates will cover:

1. credit hold before fulfillment;
2. fulfillment capacity pending;
3. partial fulfillment;
4. invoiced SalesOrder before Receivable creation;
5. Receivable with due ScheduledWork pending;
6. overdue before CollectionCase creation;
7. CollectionCase assigned/contacted with follow-up pending.

## 9. Executable evidence

| Specification area | Current implementation/evidence | Status |
|---|---|---|
| SalesOrder entity | entities.py | implemented |
| Receivable entity | entities.py | implemented |
| CollectionCase entity | entities.py | implemented |
| SalesOrder StateChart | statecharts.py + topology test | implemented |
| Receivable StateChart | statecharts.py + topology test | implemented |
| CollectionCase StateChart | statecharts.py + topology test | implemented |
| direct probabilistic gating | TransitionPolicy tests | implemented |
| O2C scenarios | scenarios.py | implemented |
| durable credit / fulfillment gating | Resource-backed fulfillment reconciler + credit/fulfillment scenario tests | implemented |
| invoice-to-receivable causality | idempotent Receivable creation after durable SalesOrder(invoiced) | implemented |
| due / overdue scheduling | durable mark_due / mark_overdue ScheduledWork + cancellation on collection | implemented |
| collection assignment / follow-up | CollectionCase + collection_agent Resource + promise follow-up ScheduledWork | implemented |
| partial-fulfillment execution | explicit partial_fulfillment runtime path + tests | implemented |
| finite scenario recovery | credit tightening / fulfillment outage tests | implemented |
| restart equivalence | due ScheduledWork rebuild test | partial |

## Promotion decision

Current status: **Partial**.

Reference promotion still requires broader restart-equivalence evidence across invoice→receivable, overdue→CollectionCase, and collection follow-up boundaries, plus final end-to-end evidence review.
