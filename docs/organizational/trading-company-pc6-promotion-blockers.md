# Trading Company PC6 — Falsification findings and promotion blockers

**Status: PC6 NOT PROMOTED.** Warehouse Fulfillment remains PC5 on main. The promotion attempt [PR #394](https://github.com/brunolnetto/sose/pull/394) was closed without merge after two P1 semantic failures and frozen-v1 test regressions.

## Blocker A — ingress without a consumer-owned business effect

**Tracker:** [#395](https://github.com/brunolnetto/sose/issues/395).

The current Trading Company customer path pre-seeds the Warehouse Fulfillment order before publishing `o2c.fulfillment_requested.v1`. The consumer checks the fixture's state rather than materializing/advancing an order based on validated immutable payload fields.

**Required evidence:** Publish an order request in an environment without a pre-seeded WF order. Reconstruct the order from the message payload, reject mismatched fulfillment IDs/SKU/quantity, prove duplicate delivery is idempotent, and survive database restart. Consumer must not mutate O2C private state. Only then add `o2c.fulfillment_requested.v1` as audited executable ingress.

## Blocker B — business effect before durable egress

**Tracker:** [#396](https://github.com/brunolnetto/sose/issues/396).

After processing `warehouse.inventory_reserved.v1`, the domain can persist a shipped fulfillment order before the Python caller publishes `warehouse.inventory_consumption_requested.v1` and `warehouse.dispatch_ready.v1`. A crash at that boundary can strand real shipped work. The current staged restart harness reconstructs stages from a pre-existing Python tuple rather than independent durable source truth.

**Required evidence:** Inject failure after shipment and before each egress publish. Close and reopen the operational store, with no previous Python stage list. Reconcile both outbound contracts from immutable occurrences or transactional outbox facts, preserve deterministic causation and IDs, ensure WM stock is consumed exactly once, and verify Logistics receives exactly one dispatch effect. Include stale-worker fencing and at-least-once retry cases.

## Blocker C — frozen-v1 lifecycle and code evolution

**Tracker:** [#397](https://github.com/brunolnetto/sose/issues/397); [proposed ADR-0007](../architecture/decisions/adr-0007-versioned-scientific-freezes.md).

The v1 official scientific freezes pin `src/sose` and the environment, and Manufacturing separately pins the process manifest. PC6 promotion changes that source tree, so unconditional live-HEAD freeze tests correctly fail. The solution must **not** rewrite frozen manifests: replay v1 in isolated pinned-source CI and permit current code evolution only under explicit versioned guarantees.

## Explicit promotion sequence

1. Fix and TDD-test the inbound business effect against durable payloads (#395).
2. Fix and chaos/restart-test recoverable outbound egress (#396).
3. Implement isolated historical freeze replay without weakening any v1 hash (#397).
4. Re-run standalone Warehouse Fulfillment PC5 conformance, composed stock ownership tests, both customer and replenishment paths, SQLite/PostgreSQL interoperability as supported, worker death, stale fencing and duplicate delivery.
5. Only then propose a new PC6 ProcessManifest update with named ingress/egress and exact test/source provenance. Keep all other business process domains at their current evidenced maturity until independently audited.

**Non-claims:** Existing PC6 smoke/restart tests alone do not establish business-effect-driven ingress or restart-safe egress at every fault boundary. The three published A0 organizational synthetic experiments prove separate scientific mechanics, not the composability of Trading Company.
