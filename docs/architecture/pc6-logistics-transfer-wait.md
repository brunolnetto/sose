# PC6 Logistics transfer: evidence-backed durable wait (#489)

The original customer-demand composition invokes
`logistics.reconcile_transfer()` while a shipment is at the origin hub or
already in transfer. A returned `False` has **both** transient finite-resource
causes and potentially genuine invalid-state causes.

Before this change, the composition layer raised terminal
`RuntimeError("logistics transfer failed")` for either case, even when
the authoritative resource ledger contained a pending transfer vehicle,
destination dock or hub-queue request.

## PRD / TRD

1. Preserve an already-ACKed, original `composition.deliver_shipment`
   Command and its `effect_id`/causation when finite capacity is busy.
2. Interpret transfer `False` as **typed `DurableResourceWait` only if**
   durable evidence is found: an ungranted `transfer_vehicle` or
   `destination_dock` request, an outstanding origin queue `StoreGetRequest`,
   or destination hub queue `StorePutIntent`.
3. If there is **no** durable pending request, the original terminal
   `RuntimeError` remains. A program bug is never silently reclassified
   as a capacity wait.
4. Destination hub queue insertion is itself idempotent: if a previous
   attempt left one durable `StorePutIntent` while the queue stayed full,
   `logistics.reconcile_transfer` uses `engine.stores.ensure_put` to reuse the
   **same item identity**, never calls `put` again and never duplicates a
   physical arrival or shipment DomainEvent. Multiple blocked retries remain
   pending until capacity is released.
5. The reference synchronous demo retries the typed wait within its
   preexisting bounded window; recurring production recovery defers the
   **same command identity** to its next slot, without repeated ACK,
   guaranteed time travel or wall-clock-based logical progression.
6. No global mutex/clock, new synthetic reservation, capacity increase or
   skipped concurrency test; SimPy remains ephemeral.

## Falsifiers

`tests/unit/sose/composition/test_legacy_durable_resource_wait.py`
creates an actual one-slot transfer-vehicle contention and an independently
contended destination dock, then releases the blocker. Both cases must
recover the shipment to delivered with uniquely certified effects. A
negative stub that returns `False` without any pending durable demand must
still fail terminally.

Existing `tests/integration/sose/examples/logistics/test_logistics_backpressure_replay.py`
covers persistent destination-store put under finite queue saturation;
this PR integrates the composite intent's evidence-based wait decision.

## Stop rule

This is **not** a proof of full physical native SimPy conformance (#477)
or completion of intermittent PG #468 / #489. Real multi-worker PostgreSQL
overlap, restart fault injection, per-organization clocks and no duplicated
physical slots still require their existing bounded CI gates and scientific
qualification. Do not change historical frozen v1 traces or OD-5a admission.
