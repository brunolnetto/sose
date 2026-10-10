# PC6 two real customer paths — PRD / TRD / ADR

## PRD
Replace fixed single-reference orchestration with an opt-in ability to execute two separate complete PC6 customer-demand paths in the same durable OLTP store. Each path must execute the domain engines/statecharts rather than fabricated terminal states, and equal prices must not result in order/payment/journal identity aliasing.

## TRD
`run_customer_demand_path(persistence=None, instance_key=None)` retains the entire legacy default. Supplying an injected store requires a nonblank explicit instance key. Seed scoped O2C, WM, Logistics, Payments and R2R reference entities; composed WF identity is already derived from order ID and therefore naturally unique. The second path starts no earlier than the store's authoritative simulation logical position.

Red-first Memory and SQLite test exercises **two full paths sequentially** in one persistence: 16 boundary messages and effects, four certified terminal outcomes per order, durable settlement bindings, real domain events, and restarted SQLite audit. Default no-arg regression checks stable historical order/correlation IDs.

## ADR and limits
Sequential execution is an intentional first falsification target. Separate business identities with shared logical clock and resource/store *definitions* do not demonstrate parallel worker execution, physical server failures, or workload-scoped scheduling fairness. A further PostgreSQL multi-process test must interleave the domain statecharts under contention and worker death. The earlier #426 test already falsifies the isolated boundary protocol under two concurrent connections but uses modeled business outcomes, not these domain engines. Both proofs are necessary and neither alone closes #406.

## Gate
Python matrix, PostgreSQL, SQLite, >=95% coverage, chaos, historical organizational v1 replay, benchmark smoke, all review threads resolved. Preserve frozen canonical source/result hashes.

## Integration dependency
The prerequisite identity PRs #427 and #428 are merged into main. Run the full suite against the main merge result; this branch was originally stacked and must not be validated against an isolated pre-#427 tree.

## New immutable ingress contract
Multiple shipments cannot be safely recovered with the historical untyped `o2c.fulfillment_requested.v1` request: its payload did not carry shipment ownership. Newly scoped instances therefore publish `o2c.fulfillment_requested.v2`, which adds an explicit `shipment_id` field. The handler and reconciler check the durable parent message, existence of the targeted shipment and the absence of a second v2 owner. Default no-key runs still produce the unchanged v1 message and payload, with the previous single-shipment legacy fallback. This is an opt-in versioned protocol, not a rewrite of published historical facts.

## Authoritative clock recovery
Sequential flows uncovered an invalid SimPy rebuild at an inbound command's later due time instead of the last durable `SimulationPosition.logical_time`. Logistics now reconstructs SimPy at the exact persisted boundary, materializes durable resource leases, then advances the backend to the command time. The strict core RuntimeRebuilder invariant is not relaxed; this avoids turning physical command acceptance order into a false recovery checkpoint.
