# Cross-Reference Architecture Audit

## Scope

This audit was performed after fourteen domains had reached **Reference
implementation** status and after resource, ScheduledWork, Store-selection, and
restart mechanics had been promoted into shared runtime/testing APIs.

The audit asks a narrow question:

> are Reference implementations still reimplementing durable mechanics that the
> core now owns?

It is not a re-evaluation of each domain's business semantics.

## Findings and resolutions

### 1. Normal Resource lifecycle drift

Several references still implemented local combinations of:

- scan ResourceDemand;
- scan ResourceReservation;
- create request if absent;
- run backend;
- locate reservation;
- cancel pending then release raced grants.

Affected code included P2P, Logistics, Cards & Payments, ITSM, Hospitals,
Construction, Order-to-Cash and related shared runtime helpers.

Resolution:

- use `ensure_requested()`;
- use `has_request()` / `reservation_for()` when a lower-level request call
  is semantically necessary;
- use `withdraw()` for pending/granted cleanup;
- remove orphan helper functions.

### 2. Preemptive Resource lifecycle drift

Manufacturing and Hospitals still duplicated preemptive request/reservation
lookup. MRO also performed direct persistence scans.

Resolution:

- Manufacturing and Hospitals now use the shared preemptive lifecycle API;
- MRO uses shared ownership queries and lifecycle operations;
- MRO deliberately retains low-level `request(..., on_acquired=...)` where the
  terminal-state acquisition callback is part of its crash-safety behavior.

The exception is about callback semantics, not lifecycle ownership.

### 3. ScheduledWork lookup/cancellation drift

ITSM, Construction and Record-to-Report still searched
`persistence.scheduled_work()` directly.

MRO had a separate requirement: cancel every outstanding command for one
WorkOrder rather than one named lifecycle.

Resolution:

- ITSM, Construction and R2R use `find_pending()` / `cancel_pending()`;
- `DurableScheduler` now exposes `pending_for_target()` and
  `cancel_target()`;
- MRO uses target-wide cancellation.

No Reference implementation now owns ScheduledWork persistence mechanics.

### 4. Store selection lifecycle

The audit found no remaining Reference usage of the legacy
`engine.stores.result(...)` selection-recovery pattern.

Raw persistence inspection of Store results remains allowed when it represents
domain evidence or idempotence checks rather than selection lifecycle ownership.

### 5. Documentation status drift

Procure-to-Pay still called itself a Reference candidate and Manufacturing did
not expose a direct README status declaration. Architecture documentation also
still described MRO as the sole reference and counted thirteen references.

Resolution:

- P2P and Manufacturing README status is aligned;
- blueprint introduction points to the executable reference catalog;
- consolidation inventory is updated to fourteen;
- conformance now requires each Reference README to state its status directly.

### 6. Test hygiene

Two Order-to-Cash invariant tests used non-raw regex string literals and emitted
Python warnings.

Resolution: use raw regex literals.

## Guardrail

`tests/test_reference_architecture_audit.py` scans all packages in the
Reference catalog and rejects reintroduction of:

- local normal/preemptive resource lifecycle helpers;
- direct manager `cancel_pending` cleanup;
- legacy `stores.result` selection recovery;
- direct `persistence.scheduled_work()` traversal.

The guardrail intentionally does not ban lower-level APIs when a domain needs a
feature not represented by the convenience lifecycle method, such as MRO's
`on_acquired` terminal cleanup callback.

## Deferred questions

### Monetary representation

Multiple financial references still use Python `float` for monetary values.
Credit & Loans now allocates installment rounding in integer cents, but a shared
Money/value-object abstraction is not introduced by this audit.

That requires a separate design decision covering currency identity,
serialization, rounding policy, persistence compatibility and migration.

### Domain-specific causal reconcilers

Cross-entity rules remain intentionally local:

- Claim -> Reserve -> Payment;
- SalesOrder -> Receivable;
- AccountingPeriod close prerequisites;
- Flight -> Aircraft;
- Loan -> Installment -> Payment;
- construction measurement/completion.

Their retry shapes may resemble each other, but their meaning is domain truth and
must not be generalized merely to reduce line count.
