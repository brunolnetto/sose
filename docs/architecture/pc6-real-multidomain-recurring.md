# PC6 real four-domain recurring recovery — PRD / TRD / ADR

**Scientific issue:** #406. **Status:** proposed experimental falsifier, not promotion.

## PRD

Prove that two operationally independent organizational recovery jobs run the real PC6 handlers through Logistics, Order-to-Cash, Cards Payments and Record-to-Report after a worker dies immediately after the authoritative Logistics ACK/Command commit, before the real Logistics transition. Warehouse/O2C bootstrap operations must be genuine, not manually fabricated terminal states. Preserve an independently shared finite physical pickup pool.

## TRD

The bootstrap uses `run_customer_demand_path` and real PostgreSQL boundary transactions until `composition.deliver_shipment` has been durably staged by the actual consumer. The worker calls `os._exit(79)` directly; another database connection must reconstruct pending work without Python stack state. The control run interrupts at the same contract boundary using a test exception, then proceeds through the identical recurring recovery scheduler. Every recurring call uses distinct durable writer epochs and per-organization checkpoints, with `max_actions=1` to expose ordering pathologies. Organization B advances on its own schedule while A is absent; A later catches up without resetting its simulation position. Both share an authoritative `pickup_courier` PostgreSQL temporal resource pool, capacity one.

After all recurrent steps, assert genuine final shipment delivered, sales order invoiced, card payment settled and journal entry posted. Compare snapshots including full entities/events/certificates/ACK consumptions, typed DAG audit, job triggers, scheduled work, stores, resource allocations and independent organizational clocks to a reference control run. Emit machine-readable SHA-256 digests in `pc6-real-multidomain-recurring.json`, uploaded with `if-no-files-found: error`.

## ADR

Unlike protocol-level experiment #447, this one executes production `_execute_pending` domain handlers. It deliberately begins after the accepted Logistics ingress to isolate a real-domain continuation; this is **not** full proof for all 24 `publish/claim/ACK/Command/certificate` fatal cuts in real domains. This test must either pass strictly or reveal actionable causal/temporal discrepancies — never silently synthesize success states or drop immutable data.

Other remaining issue #406 falsifiers: simultaneous stale worker leases, process death at every boundary window with real statecharts, PostgreSQL/network failure during uncertain COMMIT, physical instances with capacity >1, repair/preemption/fairness, cross-organization mixed legacy/authoritative allocation.
