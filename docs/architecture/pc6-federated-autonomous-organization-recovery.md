# PC6 — Federated recurring organization recovery (PRD / TRD / ADR)

Status: **experimental opt-in implementation and falsification gate**; scientific issue #406 remains outstanding. No historical v1 bundle, public domain default, or dataset fingerprint is modified.

## PRD: independent failure domains

A recurring worker must reconstruct **each organization from its own authoritative operational store**, rather than replay an in-memory stage list or advancing a global simulation clock. Failure of organization A after a real Logistics business-effect commit must neither roll back B's recurring checkpoint nor let B double-book their shared physical courier. A's restart must reuse its unfinished slot identity, reconcile the exact certified business effect, and release its reservation. B must then progress at its own logical slot, preserving its own certified effects and causal predecessors.

Evidence must include actual terminated worker process (not just an exception), separate PostgreSQL connections and operational namespaces, shared PostgreSQL physical resource identity, bounded durable waiting, and canonical comparison to a fault-free reference.

## TRD: stores, temporal positions, and causal invariants

- **Operational partition**: one PostgreSQL namespace and one fenced writer epoch per independently schedulable organization. Each retains its own `SimulationJobState`, completed trigger ledger, `SimulationPosition`, boundary deliveries, domain entities and immutable business certificates.
- **Physical partition**: a distinct, shared PostgreSQL namespace contains `PostgresTemporalResourceLedger` reservations, immutable resource events, causal links, per-organization resource clocks and deferred intents. It is shared across organizations whose actual physical resource has the same `ResourcePoolContract.address()`.
- **Authorization**: the organization validates its own writer epoch while it admits a booking in the shared ledger. At the real Logistics pickup, a shared-pool authorization transaction holds the **pool-scoped lock** through the organization's real statechart transition commit. The authoritative resource ledger—not SimPy—owns that pickup allocation.
- **Crash window**: the shared booking may commit before the organization UoW; the organization certificate may commit before the shared release. On restart, the original immutable effect ID and causal boundary link are re-read. `reconcile_certified` releases only linked, certified work owned by that organization; otherwise an unfinished command remains pending.
- **Scheduling**: `OrganizationRecoveryFleet` dispatches multiple existing `RecoverySchedule` objects, but persists no shared cursor or coordination lock. Each schedule can instead be invoked from a separate worker process; a dispatch failure is reported without preventing another organization's due slot. Distinct operational ownership is mandatory.
- **Time**: the scheduled timestamp identifies the job occurrence, `observed_at` governs real lease expirations, domain `SimulationPosition` governs rebuilt engine state, and the resource ledger's `logical_time(organization_id)` governs its independent physical planning position. Never substitute one clock for another.
- **Causality and identity:** the domain command/effect ID is locally deterministic **within its operational namespace**. Two independent organizations may legitimately generate the same local ID. In federated mode, the physical reservation ID is `deterministic_id("federated-authoritative-resource-effect", operational_namespace, local_effect_id)`; single-store mode retains the original ID unchanged. A durable link retains both IDs, scope/organization, source boundary causation and correlation. An unqualified local ID must never index a global physical pool.
- **Audit equivalence:** compare the full resource snapshot, domain event IDs/causation, business certificates, positions, and scheduled-trigger history against a baseline. Separate tests deploy different random operational namespaces, so only the derived deployment-scoped physical UUID is canonicalized to the same semantic (organization, local effect) identity, and reservations/events are re-sorted **after** normalization. Preserve every causation edge, capacity interval, status and physical event. Exclude physical retry attempts/worker epochs, not business data.

### Executable falsification

`tests/e2e/sose/persistence/test_postgres_pc6_federated_recovery.py` seeds the **real Logistics statechart** and durable boundary consumption in two distinct organizational stores; gives both a single shared courier; runs the same scheduled jobs with and without an OS-killed worker immediately after its real domain certificate is committed (before resource release); and compares business and resource truth after fresh-connection recovery. The uninterrupted run and killed run must have the same canonical reservations, causal events, entity versions, certificates, job cursors, and physical-resource organizational clocks. A separate SQLite integration test verifies the dispatcher does not starve B when A's trigger throws.

Run with `SOSE_TEST_POSTGRES_DSN` and the existing `postgres integration` workflow. Preserve the existing Python matrix, chaos gates, coverage floor, and frozen Manufacturing/O2C/MRO replays.

## ADR: selected isolation, rejected shortcuts, open scientific gates

**Selected:** separate operational namespaces with independent writer epochs and recurring checkpoint identity; one intentionally shared authoritative ledger for truly shared physical resources. This preserves the previous single-namespace opt-in API when `resource_persistence` is omitted.

**Rejected:** one global `claim_writer` for all organizations; independent copies of a physically shared capacity-one pool; a fleet-wide logical clock; claiming SimPy resource slots separately from authoritative PostgreSQL; treating boundary ACK as a business certificate; using a happy-path restart without actual process termination as sufficient proof.

**Transaction boundary:** resource-ledger and domain-state transactions use different PostgreSQL connections. They do not form a distributed atomic commit. The invariant is recoverable effect ordering with durable linkage, guarded authorized pickup, and re-entrant certification reconciliation. A permanently lost worker and external side effects still require their own failure models.

**Remaining promotion blockers:** no proof yet for mixed legacy/authoritative physical allocations, repeated SIGKILL across every business-effect/claim/checkpoint window, a full two-organization multi-domain O2C/MRO composition, pooled capacity >1 with physical instance outage and preemption, network split/DB restart, cross-organization typed causal graph audit, or fleet-wide observability under continuous load. Do not use this test to certify those properties or to mark #406 completed.
