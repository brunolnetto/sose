# OD-5a #477 — native SimPy physical arbitration promotion (RED gate)

**Status:** Draft / RED falsifier. **Scientific admission:** `NOT_ADMITTED`.
**Parent:** [#477](https://github.com/brunolnetto/sose/issues/477), scientific successor [#461](https://github.com/brunolnetto/sose/issues/461).

## Counterexample

The current `execute_od5a_claimed_tick` uses `SimPyBackend.schedule_at`
to dispatch a callback. The callback calls `_advance_od5a_in_uow`, whose
`_dispatch` chooses an `execution:N` or `coordination:0` slot in the
durable reducer. A successful scheduling callback therefore proves neither
an ephemeral SimPy resource request nor its physical acquisition.

The RED test
`test_od5a_recurring_physical_grant_requires_actual_native_simpy_acquisition`
asserts a first persisted execution-slot allocation corresponds to a real
`SimPyBackend.request_resource` and `on_acquired` callback. Baseline and
capacity-only are both exercised. It should **fail** on this initial
implementation: a green test without a native acquisition is an invalid
promotion signal.

## Next implementation slice

1. Reconstruct the ephemeral native physical pool from authoritative
   `ResourceDefinition`, `ResourceReservation`, and pending demands,
   respecting unique per-instance identity and the organization's own
   logical clock. No `simpy.Environment` or process is persisted.
2. Submit native requests through SimPy; do not claim a SOSE physical
   reservation before the backend has a matching native grant. Keep the
   authoritative commit fenced by the durable recurring lease/epoch.
3. On legitimate contention, persist an idempotent wait/retry and preserve
   the exact causal identity and position. Define fair ordering and
   preemption/release for finite capacity >1; prove no double occupancy.
4. Compare the native continuous execution, independent durable reducer and
   crash/recovered native adapter in **all five preregistered arms** with
   full canonical DAG, physical occupancy intervals, transition cost and
   model/source identities. Crash at grant, commit and release boundaries.
5. Independently test PostgreSQL server interruption, network timeout and
   unknown COMMIT outcome. Publish a hash-addressed *non-confirmatory*
   recovery bundle only after all gates and independent review pass.

## Scope and stop rule

This first PR is a **RED test and specification**, not an implementation of
native arbitration. It is intentionally **not** merge-ready while the
new test fails; CI must not be weakened or the test marked xfail/skip to
improve status. It must not modify frozen Manufacturing/O2C/MRO v1
science, certify an ACK as a terminal effect, claim a five-arm result or
change `NOT_ADMITTED` to `ADMITTED`.
