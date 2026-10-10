# PRD / TRD / ADR — authoritative temporal organizational resources

Status: opt-in PostgreSQL reference implementation; not a full PC6 organization promotion. Issue #406 stays open. Frozen Manufacturing/O2C/MRO v1 protocols remain unchanged.

## PRD — operational semantics

Resources are finite, organizationally scoped, and time-bounded. A declared pool has a fixed nominal integer capacity; an instanced pool has that many named indivisible physical resources. At every half-open time interval [start,end), the actual occupied capacity must never exceed the finite available supply. An organization-local pool is independent of another organization's identically named pool. A shared pool is contested across organizations. An individual physical instance cannot be double-booked, even if a different instance in its pool is free.

A booking is identified by an immutable reservation ID, request owner, scope-resolved address, quantity, priority, and planned interval. A reservation's *occupied historical prefix* is authoritative: releasing or preempting it does not rewrite past occupancy. Outages constrain the effective supply over a declared interval; recovery ends unavailability at a recorded time.

## TRD — PostgreSQL authoritative boundary

Added `PostgresPersistence.temporal_resources()` with an explicit, additive `PostgresTemporalResourceLedger`. The service uses namespace-qualified PostgreSQL tables:

- `resource_pool`: immutable typed pool definition.
- `resource_booking`: unique reservation ID, half-open time bounds, quantity/physical instance, priority, status, and terminal cutoff.
- `resource_outage`: immutable fault identity and temporal window, optional recovery cutoff.
- `resource_event`: immutable event IDs, actions, explicit causal predecessor IDs and logical timestamps.

One transaction holds the PostgreSQL advisory *transaction* lock for the relevant **pool**, checks availability across all change points in the request interval, updates reservations/outages and emits causal events atomically. Different organizational pool keys do not require a global worker lock. Historical resource occupancy is reconstructed from persisted intervals and stop times, never from SimPy's ephemeral queue.

Allocation retries with an identical ID are idempotent after a lost acknowledgement or worker death. Reusing an ID with incompatible arguments fails. Lower numeric priority is higher importance; `preempt=True` deterministically ends only already-active, lower-priority overlapping reservations when capacity can be made available. A failed candidate allocation must roll back all tentative changes.

An outage is recorded with an immutable identity and atomically terminates all overlapping active bookings for its affected pool or physical instance. Another instance remains available. Recovery must occur within the original outage interval and is itself an immutable causal event. Repeated identical release/fault/recovery is idempotent; conflicting retry semantics raise.

### Falsification matrix

| Hypothesis | Adversarial evidence |
| --- | --- |
| Capacity=2 admits at most two overlapping unit bookings | Three independent PostgreSQL workers start at a barrier; exactly two succeed |
| Overcommit during the middle of a requested interval is prevented | Distinct interval change-point tests, including touching intervals |
| A physical instance is exclusive while distinct instances can operate | Collision rejection, concurrent unaffected instance, recovery |
| Higher-priority work can preempt a lower-priority active booking | Explicit terminal cutoff and linked causal events |
| Faults and maintenance remove only affected availability | Outage and recovery interval tests, different physical instance unaffected |
| Lost worker acknowledgement does not duplicate allocation or event | Spawned worker executes COMMIT then exits with `os._exit(17)`, parent reopens and retries |
| Recovery preserves causal truth, not merely final utilization | Baseline vs killed-worker full normalized snapshot and canonical SHA-256 digest |
| Committed causal events do not reference nonexistent internal predecessors | Ledger `audit()` reconstructs allocations/outages and validates causal graph and historical capacity |
| Legacy examples and frozen history are unchanged | Python matrix, >=95% coverage, PostgreSQL, chaos, runtime benchmarks, historical organizational v1 replay |

## ADR — tradeoffs, restrictions and next experiments

1. The new ledger is **authoritative for its own PostgreSQL resource transactions**. It is not yet atomically coupled to arbitrary domain state transitions or the existing `EnginePersistence` record-based UnitOfWork. No claim of exactly-once external effects or full PC6 causal equivalence is justified until a shared commit/UoW boundary or durable orchestration handshake is demonstrated.
2. Pool-scoped PostgreSQL advisory transaction locks are synchronization aids. Business truth is in durable rows and immutable events. PostgreSQL hash-key collisions can cause accidental extra serialization, not double allocation. No global resource mutex is introduced.
3. Capacity refers to deterministic integer units; named instances are exclusive. A declared interval of unavailability is modeled, but recurring calendars, shift schedules, certification/skill compatibility, transport/location constraints and continuous fractional capacity are **not** implemented in this slice.
4. Preemption is explicit and conservative: no retroactive rewrite of historical activity, no equal/higher-priority victim, and no preemption of reservations whose planned start lies later than the incoming start. Sophisticated interruption/resume, priority aging, deadlock policy and compensation are deferred.
5. The causal comparison exercises real PostgreSQL transactions and loss of worker process after commit. The independent domain statecharts, global logical-time ownership, network partitions and database server crash/restart remain separate PC6 promotion gates.

**Follow-on promotion:** integrate a durable reservation ID and its causal event in each PC6 domain work intent; commit reservation transitions with domain events (or formally prove the reconciliation protocol), then run baseline versus worker-kill/network/database fault matrices on actual concurrent PC6 statecharts and compare entity/event/certificate/resource ledgers plus per-organization temporal positions. #406 remains OPEN.
