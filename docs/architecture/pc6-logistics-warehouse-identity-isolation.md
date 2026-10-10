# PC6 Logistics + WM identity isolation — PRD / TRD / ADR

## PRD
Two independent organizational instances must be able to seed Logistics shipments and Warehouse Management facilities/stock in the same authoritative persistence without identity collisions. The historical v1 default instances, hashes and evidence must not change.

## TRD
* Opt-in nonblank `instance_key` for `warehouse_management.seed_reference`, added to deterministic keys of sites, docks, forklifts, truck, stock and WM shipment.
* Opt-in nonblank `instance_key` for `logistics.seed_reference`, added to the deterministic shipment key. Keep shared courier, dock, FIFO store *definitions* as the current warehouse-wide default; this PR does not implement organizational resource tenancy.
* Derive delivery attempt identity and relationship from an explicit `shipment_id` parameter, defaulting to the legacy key for no parameter and for the legacy shipment ID. Reject contradictory attempt ownership and use the scoped lookup in PC6 recovery.
* Falsification: seed two equal-input WM/Logistics instances; issue real WM inventory reservation events with explicit disjoint stock references, instantiate both delivery attempts, restart SQLite, verify neither reserved stock nor attempt entity is aliased. Assert frozen legacy IDs unchanged.

## ADR
Identity is a stable per-fixture deterministic key, not money/unique fixture lookup. Only reference data changes; no external effects. Legacy no-argument call preserves byte-for-byte identity. Creating isolated facilities does not mean segregating scheduler/worker/SimPy state or resource capacity. A subsequent separate PR must exercise full statecharts under multiple orders and evaluate resource-sharing semantics before PC6 scientific promotion.

## Gate
Exact HEAD Python 3.12/3.13/3.14, PostgreSQL integration, coverage >=95%, chaos, benchmarks, historical v1 replay and resolved reviews. Keep issue #406 open.
