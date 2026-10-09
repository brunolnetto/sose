# PC6 O2C causal recovery — PRD / TRD / ADR

Status: narrow reference-domain continuation for issue #406. This work does not authorize an end-to-end exactly-once claim, nor deploy standalone Payments/R2R workers.

## PRD

An accepted `logistics.delivery_completed.v1` message must be automatically claimable only by the O2C owner of that contract. Death before invoice, after invoice but before receivable, or after receivable but before acknowledging *business completion* must not lose the order or cause duplicate invoicing. A restarted SOSE recovery trigger must reconstruct `o2c.payment_requested.v1` from committed O2C truth without relying on the old stage list.

## TRD

1. Consume Logistics delivery through scoped O2C contract registry. The ACK records a deterministic durable `composition.complete_external_fulfillment` Command; it is not a proof of business application.
2. After restart, search consumed boundaries for still-pending supported Commands and apply idempotent transitions to SalesOrder(`invoiced`) and Receivable(`open`).
3. Reconstruct outbound payment request only if the inbound delivery was consumed, its Command no longer exists, the SalesOrder has `invoiced` status, and a consistent open Receivable exists with matching order ID, amount, currency.
4. Reference PC6 assumes one matching card payment entity. If zero or more than one persisted match exists, fail closed; do not guess. Real multi-order integrations must carry an explicit order-to-payment binding.
5. Bound new message creation by the remaining trigger action budget. Replaying an identical request must preserve message ID, causation, payload hash and original logical timestamp; contradictory existing facts must raise.
6. The recovery runner does not claim the Payments contract; another destination owner is required.

## ADR

- Adopt durable *state*-driven O2C continuation, not in-memory stage replay.
- Preserve frozen v1 payloads and contract versions for scientific replay.
- No new global persistence table until the receipt/semantic completion model has an approved ADR; absence of the Command is still only the reference implementation's completion convention.
- Do not assert complete PC6 causality until Payments, R2R and multi-worker PostgreSQL crash/network convergence tests are implemented.
