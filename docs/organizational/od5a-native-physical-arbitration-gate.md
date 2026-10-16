# OD-5a #477 — native SimPy physical arbitration promotion (RED gate)

**Status:** RED reproduced; first native-grant slice implemented (not yet certified). **Scientific admission:** `NOT_ADMITTED`.
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
`SimPyBackend.request_resource` and `on_acquired` callback with exactly
equal `resource_name` and `request_id`. The actual SQLite persistence
boundary is instrumented to prove native capacity remains occupied *through*
the durable write, not just when a callback ran. A second negative test
suppresses the native callback and refuses to let the durable source advance.
Baseline and capacity-only are both exercised. The earlier
[RED CI evidence](https://github.com/brunolnetto/sose/actions/runs/38898074065)
proved the original scheduling-only implementation could not pass.

### Bounded native-grant implementation

The recurring job now creates a fresh SimPy PriorityResource for **each named
authoritative capacity-1 instance** using definitions and reservations read
from one UoW. It previews the next reducer transition in a **discardable**
physical ledger, without persisting its events or proposed reservations.
Reconstruction, speculative native releases, and real `on_acquired` grants
for the preview's **exact** `resource_name`/`request_id` are processed
**outside all database transactions**. Only then does a new fenced UoW
revalidate the complete source state and that organization's physical ledger
and atomically commit the source event, actual reservations/releases and job
checkpoint. During the commit the native adapter performs only identity and
capacity checks; it never advances callbacks under a database write lock.
Newly acquired native leases remain held through durable commit.

Speculatively released native leases are not durable releases: if the commit
fails, the ephemeral backend is discarded and a future runner reconstructs
the unchanged authoritative occupancy. An unadmitted blocked native request
causes a fail-closed source rollback, **not** a false completed stage or an
invented durable wait. Tests use separate SQLite writers during *both* native
reconstruction and grant callbacks, including source mutation to prove the
last-moment causal fence rejects stale plans.

The independent reference reducer remains unchanged. This is a **bounded
admission fence**, not proof that the native backend chooses optimal instances,
schedules the whole resource pool, persists fair queues, handles foreign
capacity contention, or recovers from uncertain PostgreSQL COMMIT.

### #477 follow-on: native finite execution pool (capacity N)

Each independently named SOSE physical instance `execution:N` still retains
its own authoritative `ResourceReservation` and capacity=1 definition.
The ephemeral recurring adapter additionally derives a native SimPy
`PriorityResource` named `<organization>/execution-pool` whose
**capacity is exactly the number of durable physical execution instances**
(1 for baseline, 2 for expanded-capacity arms). No extra pooled reservation,
global shared lock or SimPy object is persisted.

Every execution lease must hold **two native grants**: its specific
`execution:N` instance and one capacity unit from the shared native
execution pool. Reconstruction claims matching pool and instance units for
all pre-existing authoritative reservations. During a completion, both native
leases are released outside any database UoW; a new request may re-acquire
both. Before committing a new durable instance lease, the fenced adapter
verifies both matching native grants are still occupied. SimPy pool requests
use stable deterministic IDs derived from the durable physical request.

The bounded falsifiers exercise overlapping work requiring two independent
physical slots and `in_use=2` in a capacity-2 native PriorityResource,
reconstruction from a fresh backend each tick, and a suppressed pool callback
where the per-instance callback fires but **no durable reservation/event**
may commit. This is **local per-organization native finite arbitration**,
not a shared resource pool across organizations and not durable wait/fairness
under external contention. OD-5a remains `NOT_ADMITTED`.

## Next implementation slice

1. **Bounded capacity-N local pool implemented:** extend native resource
   arbitration to independently owned **shared** physical pools, preserving
   owner-qualified causal demand, unique instances, authoritative ledger
   and local logical clocks. Add durable pending work and resource demands;
   no `simpy.Environment` or process is persisted.
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

This PR now contains a **first implementation slice** that is eligible
for integration only if the exact-head CI, historical replay and independent
review pass. Its grant test must become GREEN by an actual native request,
never xfail/skip/mock/disabled coverage. Integrating a bounded slice does
**not** close #477: full native pool scheduling, durable wait, capacity >1
concurrency, preemption, PostgreSQL server/network uncertainty and certified
three-way equivalence remain explicit blockers. The change must not alter
Manufacturing/O2C/MRO v1 frozen science, certify ACK alone as a terminal
business effect or change `NOT_ADMITTED` to `ADMITTED`.
