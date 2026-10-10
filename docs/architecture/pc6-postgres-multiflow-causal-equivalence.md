# PC6 PostgreSQL multi-flow causal equivalence

## PRD
Demonstrate equivalence of two independent, equal-value PC6 payment chains across concurrent PostgreSQL claims and injected worker failures, compared with an uninterrupted baseline. Keep frozen v1 scientific artifacts unchanged.

## TRD
Two independent orders, shipments, payments and journals; four versioned, typed causal boundary messages per order. Two PostgreSQL connections compete for destination-scoped claims. Inject worker death after claim/before ACK, after business state/before certificate, and after ACK/before business state. Restart connections; require lease epoch fencing and no double application. Compare final entities, events, certificates, consumption receipts, binding identities, typed causal DAG, scheduled work, resource reservations, stores, containers, job states and sink checkpoints. Normalize only physical retry count, claim ownership/epoch and lease timestamps. Expected 8 messages, 8 domain events, 8 certificates, 6 typed edges, zero pending effects.

## ADR and limits
Exercise real PostgreSQL EnginePersistence, BoundaryService, SettlementBindingService and BusinessEffectService, with modeled durable terminal outcomes. This is **not** an end-to-end run of all organizational statecharts: current seed functions use fixed identities. Faults are controlled connection restarts, not external process SIGKILL, network partitions or database server shutdowns. A passing result supports only this explicit fault matrix; it does not prove exactly-once external effects. #406 remains open.

## Gate
TDD adversarial test, full CI with Python 3.12–3.14, >=95% coverage, PostgreSQL, chaos, benchmarks, historical v1 replay and reviews. Merge only when all pass.
