# PC6 / Trading Company — Explicit Settlement Identities (PRD · TRD · ADR)

**Status:** operational recovery hardening. The frozen scientific v1 payloads and event contract versions are unchanged.

## PRD — Problem and acceptance

Two different orders can have the same amount and currency. Such equality is not provenance. The current O2C and Payments recovery workers search for a *unique value-matching* card payment or journal, which fails even when two legitimate independent orders have identical prices, and risks incorrect matching if only one unrelated candidate is present.

The destination must instead be identified by **explicit durable business identity**. Accepted outcomes:
- Binding one order to one payment and one journal is immutable and independently retrievable by all three IDs.
- Rebinding any one of those IDs to a different chain fails closed.
- Binding validates the three authoritative endpoint entities and amount/currency at creation.
- Reconciliation uses the identity binding and checks correlation, amount, currency and destination existence before publishing; it never guesses from a query over equal-valued entities.
- Binding and boundary writes share a PostgreSQL namespace-scoped causal serial order; an independent worker cannot read a partially committed binding.
- Fresh SQLite reconstruction and already-open independent readers resolve the correct IDs, including two chains with equal monetary values.
- Existing v1 boundary payloads, message ID formation and frozen experiment protocols are preserved.

## TRD — Durable model and recovery sequence

`CustomerSettlementBinding(binding_id, order_id, payment_id, journal_id, amount, currency, correlation_id)` has an immutable, deterministic ID keyed by order. The generic authoritative `_State.customer_settlement_bindings` collection stores the same immutable record at **three keys**: `("order", order_id)`, `("payment", payment_id)` and `("journal", journal_id)`. A UnitOfWork verifies all keys and writes all three atomically; if any key belongs to a different record, it raises without changing the committed state.

`SettlementBindingService.bind` acquires `boundary_transaction` where supported, verifies each materialized entity, then commits all three indices in one UoW. It uses the same PostgreSQL boundary advisory lock as causal publish/claim/ACK and BusinessEffectApplied receipts. Service lookups always read through a fresh transactional snapshot, avoiding stale adapter caches.

PC6 reference seeding binds the three IDs **before** the first boundary publication. After an inbound Logistics ACK and verified O2C invoicing, the worker looks up the order's payment ID; after settled card payment, it resolves that payment's journal ID. Any missing binding or contradictory source amount/correlation is an error, never a fallback to amount matching. The externally visible v1 messages continue to contain the same `order_id`, `payment_id`, `journal_id`, amount and currency.

## ADR — Tradeoffs and scientific scope

A single three-way binding models the Trading Company PC6 reference contract; it deliberately does not impose a universal one-payment-per-order constraint on SOSE or all payment domains. Real partial payments and one-to-many journals need a domain-specific schema/versioned contract and explicit fanout, *not* hidden probabilistic joining.

Three keyed copies trade storage for authoritative O(1) lookup and mutual exclusivity. Atomicity depends on the qualified authoritative adapter; every writer must use the service's serialized boundary transaction, not bypass it through raw persistence APIs. Immutable references are independent of later mutable entity state, but downstream reconciliation still validates that the materialized money amount and currency agree with the binding.

This removes one source of causal ambiguity; it does not on its own certify exactly-once external side effects under a worker/network partition. Issue #406 remains open for PostgreSQL crash/partition falsification and durable causal DAG equivalence against uninterrupted execution.

## TDD and scientific gates

Red-first tests cover rebinding three independent identities, missing/mismatched endpoints, invalid amounts, duplicate-value parallel bindings, SQLite restart and cross-instance visibility. Integration tests construct a second same-valued independent chain and verify recovery selects the correct payment/journal. PostgreSQL interleaving tests must establish serialization. Merge requires Python 3.12–3.14, PostgreSQL, chaos, coverage ≥95%, benchmarks, historical v1 replay and resolved review threads.
