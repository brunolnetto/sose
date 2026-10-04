# Cross-Reference Architectural Audit

## Scope

This audit reviews the fourteen promoted Reference implementations after the
resource, scheduler, Store-selection, restart-toolkit, and Reference-conformance
consolidations.

The audit asks one question:

> where does Reference-domain code still implement durable runtime mechanics
> that the core already owns?

It intentionally does not normalize domain semantics merely because two domains
have similarly shaped workflows.

## Findings fixed in this change

### Resource lifecycle drift

Six References still carried local normal-resource helpers that manually:

- inspected ResourceDemand/ResourceReservation;
- submitted requests;
- looked up grants;
- cancelled pending work;
- released reservations.

Affected domains:

- IT service management;
- Hospitals;
- Construction;
- Logistics & transport;
- Order-to-Cash;
- Cards & payments.

Procure-to-Pay also called the low-level resource manager directly.

All now use the promoted lifecycle API:

- `ensure_requested(...)`;
- `reservation_for(...)` when durable ownership itself is domain evidence;
- `withdraw(...)`.

### Preemptive-resource lifecycle drift

Manufacturing, MRO, and hospital procedures still manually inspected or
submitted preemptive-resource demand/reservation state.

They now use the same lifecycle vocabulary through
`DurablePreemptiveResourceManager`.

MRO exposed one legitimate gap: its race guard needs an `on_acquired`
callback so a late grant can be discarded after the WorkOrder becomes terminal.
`ensure_requested(..., on_acquired=...)` now preserves that callback for both
new and already-pending requests.

### ScheduledWork lifecycle drift

ITSM still searched and atomically deleted SLA work locally. MRO cancellation
also deleted scheduled work/commands directly.

Both now delegate to `DurableScheduler.find_pending()` /
`cancel_pending()`.

A queued backend callback may still execute after durable cancellation, but
`Engine.dispatch_scheduled()` revalidates durable ownership and treats it as
stale.

## Executable anti-drift gate

`tests/e2e/sose/testing/reference/test_reference_horizontal_audit.py` walks every Python module belonging
to every contract in `REFERENCE_CATALOG` using the AST.

Reference domains may not:

- define the retired local resource/preemption helper families;
- call low-level `resources.request/cancel_pending/release`;
- call low-level `preemptive_resources.request/cancel_pending/release`;
- delete ScheduledWork directly through persistence.

The test does not use a hard-coded domain list; the Reference catalog is its
source of scope.

## Conformance hardening

Reference capability declarations now have dependency rules.

Examples:

- preemption requires resource evidence;
- ScheduledWork requires restart-equivalence evidence;
- Store selection requires restart-equivalence evidence;
- crash recovery requires restart-equivalence evidence;
- probabilistic transitions require StateChart evidence.

This keeps the catalog internally coherent without attempting to infer business
correctness.

## Deliberately not normalized

### Store result inspection

Some domains inspect durable Store results to avoid duplicating PUT/material
effects. That is not the same as selection ownership and remains valid.

Selection workflows themselves should use
`DurableStoreManager.ensure_selection()`.

### Deterministic child identities

Reference domains retain named helpers such as `inspection_id()`,
`installment_id()`, and domain correlation IDs. These encode domain occurrence
identity and are not runtime lifecycle mechanics.

A future audit may add tests that compare helper output to EntityFactory keys,
but the helpers should not be replaced by one opaque generic ID factory.

### Money representation

Cards & payments, Order-to-Cash, Record-to-Report, Insurance, and Credit &
Loans currently contain executable monetary semantics.

The current Procure-to-Pay Reference does not: its numeric Store/Container
amounts are inventory quantities, while invoice/matching/payment remain outside
the executable slice. The dedicated money audit documents this distinction in
`reference-money-semantics.md`.

Credit & Loans already allocates installment amounts in integer cents before
projecting values into attributes, but the framework does not yet expose a
Money primitive.

The money audit fixes missing currency provenance across R2R, Insurance, and
Credit & Loans, but deliberately does not introduce a Money primitive. Such an
abstraction still requires explicit currency-scale, rounding, serialization,
allocation, and arithmetic contracts.

## Outcome

After this audit the dependency direction is enforced mechanically:

    domain eligibility / meaning
              |
              v
    promoted durable lifecycle API
              |
              v
         persistence
              |
              v
       ephemeral backend

Reference-domain code remains responsible for *why* work is legal.

Core managers are responsible for *how* durable lifecycle mechanics survive
retry, race, and restart.
