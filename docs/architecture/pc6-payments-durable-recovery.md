# PC6 Payments restart & accounting egress — PRD / TRD / ADR

Status: dependent increment on O2C recovery (PR #416). The Trading Company v1 scientific protocol remains immutable.

## PRD
The recipient of `o2c.payment_requested.v1` may crash at any statechart phase. A new authoritative worker must resume payment authorization, capture, settlement scheduling and settlement from its committed state, without duplicating capture or publishing accounting requests before settlement. No Payments worker should claim unrelated R2R/O2C deliveries.

## TRD
- After destination-scoped ACK, create one deterministic `composition.settle_customer_payment` Command. ACK is transport acceptance, not business completion.
- Rebuild ephemeral SimPy resources from durable backend-neutral reservations; drain zero-time callbacks before releasing leases.
- Distinguish `authorization_requested`, `authorized`, `captured`, `settlement_pending`, `settled`. Reconcile only missing transitions and do not rewind committed simulation position.
- A worker can die between capture's statechart transition and its `settlement_due` schedule write. Recover the schedule from the committed payment's updated logical timestamp; preserve an existing durable schedule and its original identity when present.
- Publish `accounting.entry_requested.v1` only after the requested payment is durably settled, the accepted Command has completed, the amount/currency are unchanged and exactly one matching journal destination exists in the reference PC6 catalog. Fail closed on missing or ambiguous journal identity.
- Each trigger retains the existing max-actions budget and published message causation; R2R ownership belongs to a separate consumer.

## ADR
Prefer a state-derived event continuation to repeated end-to-end replay of authorized/captured payments. Explicitly do **not** infer a generic payment-to-journal relation from matching monetary values outside the reference fixture: the long-term production contract needs a durable binding record. No universal exactly-once business-completion claims or changes to frozen scientific evidence follow from this PR.
