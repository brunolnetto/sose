# PC6 organization-scoped PostgreSQL fixtures — PRD / TRD / ADR

## PRD
Establish a credible multi-customer seed gate in a single authoritative PostgreSQL namespace. Two independent customers have equal monetary amounts but must not alias order, payment, journal, shipment or warehouse stock identities. Frozen v1 default identities must remain unchanged.

## TRD
Use the previously implemented opt-in instance_key hooks from #427–#428. Seed O2C, Cards Payments, R2R, Logistics and Warehouse Management for customer-a and customer-b; bind order → payment → journal using durable identity, never equal-valued monetary inference. Close writer, reopen independent PostgreSQL reader, and assert distinct IDs, existing entities, and bidirectional binding resolution. Run under the explicit PostgreSQL CI job.

## ADR and limitations
Keep the reference seed APIs and frozen historical protocols unchanged. This step proves persistent multi-tenant *identity isolation of domain fixtures*, not multi-order PC6 runtime recovery. Shared domain resource definitions, process-global simulation position, domain event IDs, temporal scheduling and recovery-runner ownership remain candidate cross-customer coupling points. Next falsification must actually run two scoped domain statechart flows in one namespace and compare uninterrupted versus injected-failure recovery, including domain event uniqueness.

## Gates
Python matrix, PostgreSQL, >=95% coverage, chaos, benchmarks and historical replay. Keep #406 open.
