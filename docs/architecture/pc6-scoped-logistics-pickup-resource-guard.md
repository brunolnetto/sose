# PC6 shared pickup-courier contention: scoped cross-worker guard

## PRD
A shared courier with capacity one cannot perform incompatible pickups simultaneously. The real two-customer PostgreSQL PC6 statechart falsification exposed intermittent `logistics pickup failed` under overlapping workers. Prevent a transient contention result from being interpreted as permanent failure.

## TRD
Acquire the existing PostgreSQL session-scoped `business_resource_guard("logistics.pickup_courier")` only around the pickup statechart/reconciliation, including durable scheduled-pickup resolution, capacity acquisition, state transition, and release. This operation spans multiple UnitOfWork transactions; an xact-only lock would release too early. Legacy non-PostgreSQL runtime retains its existing behavior. Keep the real PC6 two-statechart concurrency regression unchanged. Independently test the named pickup-courier guard blocks a competing PostgreSQL worker.

## ADR / limits
Do **not** introduce an organization-wide or global lock. The pickup guard is scoped to one truly shared constrained resource; unrelated resources and later shipment stages remain free to overlap. The guard is an ephemeral session mutex, **not** an authoritative reservation, calendar, durable fencing lease or exactly-once certificate. An eventual migration to `ResourceAddress` and the temporal ledger (#435/#436) must preserve the same semantics while recording occupancy durably and without implying a single pool mutex supports capacity >1.

## Promotion criteria
Exact-head CI 3.12–3.14, PostgreSQL and chaos, project coverage >=95%, runtime benchmarks, historical v1 replay and no unresolved reviews. #406 remains open until comprehensive causal equivalence under restart and worker failures.
