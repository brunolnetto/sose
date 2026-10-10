# PC6 full domain-statechart PostgreSQL equivalence — PRD/TRD/ADR

## PRD
Exercise two independently keyed customer-demand organizational flows with the actual SOSE domain statecharts and real PostgreSQL EnginePersistence. Compare uninterrupted execution with a complete PostgreSQL connection disposal/reopen between flows. Distinguish this scientific test from #426, which tested concurrent BoundaryService/BusinessEffectService protocol but modeled business outcomes rather than running full statecharts.

## TRD
Execute `run_customer_demand_path(persistence=PostgresPersistence(...), instance_key=customer-a)` followed by `customer-b`. Control uses one live PostgreSQL connection; recovered run fully closes it between customer flows and reads the final snapshot from another fresh connection. Namespaces differ but deterministic fixture IDs and application clock schedule are identical.

Compare normalized and sorted:
- Entities (state, version and attributes)
- All domain events
- Full BusinessEffectApplied receipts
- Durable BoundaryConsumption identities
- Settlement bindings by concrete IDs
- CausalAuditReport digest/edges/effect counts
- Resources, durable schedules, store items, container states, jobs and sink checkpoints.

Do not normalize payload or business identity differences. Only irrelevant physical database namespace is excluded. Expect 8 certified effects, 0 pending effects, two separate chains.

## ADR / limitations
Reconnection after one **completed** flow exercises persisted catalog independence, but is not an injected crash *during* a domain transition; nor is it concurrent worker execution. The full PC6 concurrent failure certification remains unfinished: separate OS worker deaths, DB outages and recovery between each ACK/domain state/certificate/publication step must be tested next. A matching fingerprint alone is insufficient, hence ledger comparisons. No frozen v1 hashes are altered.

## Gates
Exact-head CI, dedicated PostgreSQL job, >=95% authoritative coverage, Python matrix, all chaos gates, historical replay and review. PR depends on #429 and #428; retarget to main only after they merge.
