# OD-5a — Authoritative SQLite recovery oracle (non-confirmatory)

Tracking: [#461](https://github.com/brunolnetto/sose/issues/461). Depends on merged [#473](https://github.com/brunolnetto/sose/pull/473).

## PRD — Falsifiable question

For a **small hand-configured workload** admitted by OD-5a, can an operational actor/resource execution reconstruct after an unclean worker death from **authoritative persisted state**, preserving its own clock, causal predecessors, finite physical reservations, and completed effects exactly as if the worker had not died?

Passing this oracle is a prerequisite to the independently frozen 17,920-run design. It is not the confirmatory experiment, not empirical prediction, and not a PC6 recertification.

## TRD — Transaction boundary and durable truth

- `SQLitePersistence` (SOSE `Persistence`/`UnitOfWork`) owns every authoritative mutation. The process owns only a temporary Python scheduler/reducer instance.
- The namespaced `Entity(od5a_durable_pilot, job_id)` stores each job's own logical clock, next sequence, outstanding semantic event actions, fixed exogenous service demands, dependency signals, work queues, physical-resource occupancy and completion checkpoint.
- Engine `ResourceDefinition` assigns **capacity one to each physical execution instance** and to the explicitly configured pilot coordinator. `ResourceReservation` records granted occupancy on the same authoritative database. A semantic completion atomically deletes the reservation and enqueues work newly enabled by that completion.
- Immutable SOSE `DomainEvent` records carry `correlation_id`, `causation_id`, the full sorted parent set, a stable event ID, `model_spec_hash` and `config_epoch`. A dependency target becomes runnable **only when all registered incoming signals have been durably delivered**.
- One `advance_od5a_durable` invocation processes **one earliest action** in a single transaction: select pending action, validate logical clock and ownership, append events, grant/release reservations, update queues and persist checkpoint. A crash after commit but before client ACK means the new process discovers the *next* uncommitted action. No in-memory SimPy state is serialized.
- `initialize_od5a_durable` is idempotent with respect to a canonical admission+workload hash. A new or contradictory intervention/workload on the same job ID fails closed.
- The run reaches `done` only when all items complete and no action, queue or physical reservation remains.
- The reference recovery comparison uses the same worker-independent append-only event identity and persistent entity truth for both uninterrupted and restarted workers.

## TDD falsification matrix

- All **five** registered arms: uninterrupted execution versus one or multiple disk-close/reopen boundaries; compare full entity state, DomainEvents and authoritative resource reservations exactly.
- OS-process death after a committed semantic event, before completion acknowledgment; compare durable final truth against uninterrupted control.
- A new worker processing subsequent actions after stale client acknowledgment must not duplicate a committed event or allocation.
- No physical overlap on the same instance; every causal parent must precede its dependent record; identical `model_spec_hash` and epoch across each immutable treatment run.
- Resume under a different model or offered workload is rejected.
- Historical v1 source freezes and PR-review v1 failed scientific result remain untouched.

## ADR — limits, separation and next gates

This is an **additive reference recovery oracle**, not yet the normal OD-5a SimPy adapter's complete persistent runner and not a substitute for the existing durable SOSE scheduled-work/worker-lease/fencing system. The pilot reducer reproduces its declared physical-resource/coordination semantics by durable actions but still requires cross-adapter differential conformance, real concurrent worker fencing, per-item and per-actor time-ledger completeness, and fault injection during persistent claims and grants before promotion.

The canonical `Entity` clock is scoped to `job_id`; this avoids inventing an unnecessary shared organizational clock. The reference's physical coordinator capacity is a registered pilot assumption, not observational evidence.

**No confirmatory outcome can be published from this PR.** An independent execution freeze must fix: exact source and `uv.lock` identities, numeric Sobol bounds and physical coordinate mapping, horizon and warmup, root/replication seeds, exogenous arrival/service counter streams, cost and transition-time schedule, complete intervention manifests, admissibility, censoring, causal recovery gates, audit bundle schema, and the independent SHA-256 verifier. A freeze is invalid if any field remains `UNSET` or a numerical parameter is chosen after looking at outcomes.


## Added differential and atomicity falsification

The implementation additionally requires equality of the small deterministic operational trace (physical instance, start/end, dependency source/target and action kind) against the independent ephemeral SimPy pilot for **all five** registered arms. Merely replaying the same persistent reducer twice would be insufficient evidence of mechanism equivalence.

Separate real OS worker-kill tests target both sides of the transaction boundary: death after a durable event is committed but before acknowledgment, and death **inside the SQLite UnitOfWork** immediately after appending an event but before commit. The latter must roll back every uncommitted causal and resource effect. Two job IDs sharing one SQLite database must advance independently without cross-job reservations.


As with the datetime-backed SimPy pilot, this reference reducer rejects nonrepresentable sub-microsecond configured arrivals, service intervals, coordination latency and intervention downtime at admission. It cannot silently round a durable resource acquisition/completion into a different causal interval. Exact quantization of generated stochastic durations remains a **future execution-freeze decision**, not a post-outcome adjustment.


## Review hardening: cumulative time arithmetic

Input durations alone being microsecond-aligned is insufficient: IEEE-754 addition can yield `0.1 + 0.2 = 0.30000000000000004` and create a false causal ordering difference between the durable reducer and the datetime-normalizing SimPy backend. The durable action completion scheduler now converts both terms to integral microseconds, adds integers and only then converts the normalized absolute offset to seconds. Loss of representability above `2**53` microsecond ticks fails closed. Even below that cap, where IEEE-754 float-seconds spacing may exceed a microsecond (such as a clock at `2**33` seconds), a strict round-trip from integer ticks to seconds and back must succeed before any action is recorded; otherwise the complete UoW rolls back. A five-arm fractional service/arrival differential regression compares full physical traces against the independent SimPy pilot.


## Causal scheduling conformance

Declared dependency edges need not be lexicographically ordered. Because the physical coordinator is capacity one, the ordering of simultaneously available downstream handoffs affects resource reservations, event positions and observed completion times. The durable reducer sorts downstream station targets exactly like the reference SimPy pilot, rather than trusting JSON edge declaration order. A negative-to-positive differential fixture fixes an intentionally unsorted DAG and compares the complete physical event trace and per-item completion checkpoints.


## Cross-owner resource and compiler admission (review P1)

A capacity-one OD-5a physical instance is **not** owned solely by the reducer's entity. Every allocation consults the complete transaction-local `UnitOfWork.resource_reservations()` ledger for matching `resource_name`, including external components' grants under different request/reservation IDs. A competing lease causes the attempted semantic step to roll back atomically: no orphan causal event, duplicate instance grant or clock advance.

The reducer requires a five-arm `OD5aCompiledArm` generated by `compile_od5a_arms`, with its immutable compiler-time admission hash unchanged, matching treatment/cost/intervention. An arbitrary object carrying the same `arm_id` is rejected. The private, per-process compiler marker is **not cryptographic evidence**: a later independent frozen executable, source/lockfile digest, ModelSpec, priced intervention, seed and provenance manifest remain indispensable before running the confirmatory DOE.
