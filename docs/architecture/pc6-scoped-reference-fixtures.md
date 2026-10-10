# PC6 opt-in fixture identity isolation — PRD / TRD / ADR

## PRD
Multiple same-value customer flows must be able to seed separate O2C, Cards Payments, and R2R entities in the same authoritative OLTP store without silently aliasing the historical fixed-ID reference entities. The frozen v1 default identifiers and results must remain stable.

## TRD
Add optional `instance_key: str | None = None` to these three `seed_reference` entry points. `None` generates exactly the original deterministic entity ID. Explicit nonblank string opt-in adds a distinct segment to each ID's deterministic creation key, including R2R period/journal/reconciliation/close task. Resource definitions remain shared as before, so this is identity isolation, **not** a claim of separate resource tenancy. Reject empty and non-string keys.

TDD: two independent customer keys seeded with the same USD amount, execution of real O2C credit statechart for each, immutable CustomerSettlementBinding associations, restart from SQLite, and legacy deterministic IDs. Neither money equality nor database insertion order can substitute for identity ownership.

## ADR and scientific limits
Use the existing EntityFactory deterministic creation key rather than a new global organizational ID translation layer. Keep changes local to the actual duplicated fixture identity problem. Avoid changing the frozen v1 benchmark and published hash definitions. This first increment does **not** claim full simultaneous logistics/warehouse execution: Logistics has fixed demo delivery-attempt lookup keys, and Warehouse Management currently shares concrete facility stock and resource identities. These need separate verified engineering work before a full multi-order organizational certification can be made.

## Promotion
Full Python 3.12–3.14 test matrix, SQLite, PostgreSQL, >=95% coverage, all chaos gates, benchmark smoke, v1 historical replay, resolved review, then squash-merge and post-merge audit. #406 remains open.
