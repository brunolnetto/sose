# Reference Pattern Consolidation

## Purpose

SOSE now has fourteen Reference implementations spanning physical flow,
financial operations and lending, case management, accounting close, insurance,
airport turnaround, and aviation rotation/AOG maintenance.

This document records the first consolidation pass across those references.
The goal is not to move domain orchestration into the core. The goal is to
identify repeated durable-runtime mechanics that can be made safer and more
consistent without hiding business semantics.

## Principle

A core abstraction is justified only when:

1. the same durable invariant appears in multiple unrelated domains;
2. the abstraction can be named without domain vocabulary;
3. retry/restart semantics become safer or more testable;
4. callers still decide whether the operation is semantically legal.

## Promoted in this change

### Durable resource-request lifecycle

Repeated reference code implemented the same sequence:

    request absent?
      -> persist demand
      -> let backend grant
      -> find reservation

and, on invalidation or post-state cleanup:

    cancel pending demand
      -> release raced/current reservation
      -> converge backend
      -> safe retry

This appears in Record-to-Report, Insurance, Airports, Aviation, Construction,
Order-to-Cash, MRO, Hospitals, and other examples.

The lifecycle now belongs to:

- `DurableResourceManager`
- `DurablePreemptiveResourceManager`

Both expose the same operational vocabulary:

- `has_request(request_id)`
- `reservation_for(request_id)`
- `ensure_requested(...)`
- `withdraw(backend, request_id)`

### Why these methods belong in core

A caller still owns the business decision:

    if scenario_allows_work and entity_state_is_legal:
        resources.ensure_requested(...)

The manager owns only the durable mechanics of making that request idempotent
across the pending/reserved phases.

Likewise:

    if work_is_no_longer_legal:
        resources.withdraw(...)

does not decide *why* work is invalid. It only guarantees that no durable demand
or reservation survives the invalidation boundary.

## References migrated as proof

This change migrates four deliberately different references:

- **Record-to-Report** — terminal-state resource cleanup;
- **Insurance** — scenario invalidation and post-assessment/payment cleanup;
- **Airports** — gate/tug contention, reallocation, and departure cleanup;
- **Aviation** — crew/inspection cleanup and AOG preemptive maintenance.

The migration is intentionally not repository-wide in one patch. These four
references exercise enough lifecycle variation to validate the abstraction
without obscuring review with mechanical churn.

## Additional promoted patterns

### ScheduledWork lookup / cancellation

`DurableScheduler` now owns the lifecycle mechanics for unique pending work:

- `find_pending(entity_type, entity_id, name)`;
- `cancel(work_id)`;
- `cancel_pending(entity_type, entity_id, name)`.

Cancellation atomically removes both `ScheduledWork` and its persisted
`Command`. A backend callback already queued before cancellation is permitted
to fire later, but becomes stale because `Engine.dispatch_scheduled()`
revalidates durable ownership before execution.

Insurance, Order-to-Cash, Airports, and Aviation use this API instead of local
scheduled-work search/delete helpers.

### Durable StoreGetResult selection ownership

`DurableStoreManager` now exposes:

- `pending_get(request_id)`;
- `selection(request_id)`;
- `ensure_selection(...)`.

A committed `StoreGetResult` is authoritative even after the selected item has
left the store. `ensure_selection` first honors an existing result, then an
existing pending get, and creates a new get only when neither exists.

The abstraction deliberately does not erase store policy:

- FIFO ordering remains backend/store behavior;
- PriorityStore users may still validate the durable head before selection;
- FilterStore selection retains its durable `filter_key`.

Airports, Aviation, Insurance, ITSM, and Hospitals use the consolidated
selection lifecycle.

### Reference restart-test toolkit

`sose.testing.restart.restart_reference_runtime` standardizes only the
mechanical recovery boundary:

1. resolve logical restart time from the previous backend or persisted
   `SimulationPosition`;
2. rebuild context/engine over the same persistence;
3. construct a fresh backend;
4. reconstruct durable runtime state;
5. drain callbacks at the exact recovery boundary.

The toolkit does not compare domain state or decide equivalence. Reference tests
still own their continuation steps and semantic assertions. This keeps restart
evidence explicit while removing repeated setup boilerplate across the reference
test suites.

## Deliberately kept domain-local

The following patterns are *not* core abstractions:

- Claim -> Reserve -> Payment causality;
- SalesOrder -> Receivable creation;
- AccountingPeriod close prerequisites;
- Flight -> Aircraft dispatch reconciliation;
- AOG maintenance release;
- Construction measurement/completion gates;
- hospital admission/procedure constraints.

These are domain rules even when they use similar retry shapes.

## Architectural outcome

The intended dependency direction remains:

    domain eligibility / invariant
            |
            v
    durable manager operation
            |
            v
    persistence + ephemeral backend

The core may own durable mechanics.

The domain must continue to own meaning.
