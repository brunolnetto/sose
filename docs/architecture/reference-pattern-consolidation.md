# Reference Pattern Consolidation

## Purpose

SOSE now has thirteen Reference implementations spanning physical flow,
financial operations, case management, accounting close, insurance, airport
turnaround, and aviation rotation/AOG maintenance.

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

## Candidates for the next consolidation PR

### ScheduledWork lookup / cancellation

At least four references contain local helpers that search ScheduledWork by
target entity + command name and then delete both work and command.

This is a strong next candidate because cancellation has durable transactional
semantics. It should likely live in the scheduler/manager rather than examples.

Do not extract it in this patch because the correct API must distinguish:

- lookup only;
- cancel pending work;
- already-fired command;
- cancellation racing execution.

### Durable StoreGetResult selection ownership

Airports, Aviation, Insurance, ITSM, and Hospitals depend on a committed
`StoreGetResult` after the selected item is no longer present in the Store.

This is a real cross-domain invariant:

> once selection commits, recovery continues from the result, not from the
> original queue item.

However, selection differs across FIFO, PriorityStore, and FilterStore. A common
helper should wait until those ownership semantics can be expressed without
erasing queue-specific rules.

### Reference restart-test toolkit

The references repeat test structure for:

- continuous execution;
- persistence checkpoint;
- backend rebuild;
- continuation;
- durable-state comparison.

A testing-only toolkit is a good candidate for a separate PR. It should reduce
boilerplate without replacing domain-specific assertions.

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
