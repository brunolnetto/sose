# Chaos-test gate

The chaos-test gate is a required recovery contract for durable SOSE execution.
It complements restart-equivalence tests by killing execution at transaction
boundaries rather than only between logical ticks.

## Engine OLTP failure model

For every canonical example, the gate first records a clean three-tick control
run and counts every SQLite Engine OLTP transaction. It then replays the same
workload twice for every observed transaction boundary:

- `before_commit`: execution dies while the SQLite transaction is still open.
  Closing the process connection must leave no partial durable transaction.
- `after_commit`: execution dies after COMMIT succeeds but before the caller
  observes success. Recovery must recognize already-committed progress and
  continue idempotently.

After each injected crash the database is reopened through a fresh
`SQLiteIncrementalPersistence`, the unresolved trigger is explicitly recovered,
and execution continues. The final durable snapshot must equal the clean control
snapshot exactly.

The snapshot includes orchestration state, logical position, events, domain
entities, domain and sink outboxes, scenario state, scheduled work and referenced
commands, Resource/Store/Container state, and preemptive-resource state.

## Dual-store failure model

The gate also exercises the Engine OLTP -> Domain Store bridge using independent
SQLite databases:

1. Domain Store failure before `apply()` commits: the durable outbox records the
   failed attempt, survives restart, and is delivered successfully later.
2. Process death after Domain Store `apply()` commits but before Engine OLTP ACK:
   the mutation remains pending, replay returns `REPLAYED`, and the ACK removes
   the pending delivery without duplicating business state.

## CI contract

The dedicated `chaos-test gate` job runs on Python 3.14 and is intentionally
separate from the normal pytest matrix. It also runs the all-built-in dual-store
restart suite so every registered domain/canonical must continue across a fresh
Engine Store + Domain Store process boundary.

A green gate establishes deterministic recovery for the modeled failure
boundaries. It does not claim resilience to host/kernel/filesystem corruption,
network partitions, or backend-specific failures that are not represented by
the SQLite reference adapters; those require adapter-specific chaos suites.


## Level 2: process and backend failures

A second required CI job, `chaos process/backend gate`, moves failures outside
the SOSE call stack.

By default, local runs of level-1/2/3/4 suites execute a representative
canonical sample (`producer_consumer`, `readers_writers`) to keep feedback time
practical. Set `SOSE_FULL_CHAOS_CANONICALS=1` to run the full canonical matrix.
Level-2/4 local runs also sample only the `before_commit` phase by default; set
`SOSE_FULL_CHAOS_PHASES=1` to include both `before_commit` and `after_commit`.
For level-2 process death boundaries, local runs default to a representative
midpoint; set `SOSE_FULL_PROCESS_CHAOS=1` for first/middle/final or
`SOSE_EXHAUSTIVE_PROCESS_CHAOS=1` for every boundary. CI sets these flags so
required gates continue covering the full matrix.

### Real process death

For every canonical example, a clean run first counts Engine OLTP transactions.
The gate then samples transaction boundaries and starts the workload in a
separate Python process. The child pauses immediately
before or after the selected SQLite COMMIT and the parent sends a real POSIX
`SIGKILL`.

No Python exception handler, `finally` block, or in-process cleanup can run.
The parent opens the WAL-backed SQLite database with a fresh
`SQLiteIncrementalPersistence`, recovers any unresolved trigger, continues to
the same horizon, and requires the full durable snapshot to equal the clean
control run.

### PostgreSQL backend termination

The same five canonicals are also executed through `PostgresPersistence`.
At a representative mid-run transaction boundary, the worker publishes its
actual PostgreSQL backend PID. An independent administrative connection calls
`pg_terminate_backend()` while the worker is either immediately before commit
or immediately after commit.

The original connection is not reused. A fresh `PostgresPersistence` is opened
against the same namespace, unresolved work is recovered, and the final durable
snapshot must equal the clean PostgreSQL control.

This tests a materially different failure mode from an injected Python
exception: the database connection itself disappears underneath the runtime.

### Scope

The level-2 gate establishes recovery from abrupt process death and database
session loss for the SQLite reference Engine Store and PostgreSQL Engine Store.
It still does not model whole-node PostgreSQL failure, network partitions,
filesystem corruption, disk-full conditions, or failover between independent
database servers. Those belong to adapter/infrastructure-specific suites rather
than the portable runtime gate.


## Level 3: worker ownership and fencing under death

The `chaos fencing/worker-death gate` validates the authoritative single-writer
contract under real concurrent-worker failure.

For every canonical example and for both authoritative reference stores
(SQLite and PostgreSQL), worker A acquires a durable writer epoch and pauses
immediately after the trigger claim has committed.

Two scenarios are then exercised:

1. **Dead worker takeover** — worker A is killed with POSIX `SIGKILL`. Worker B
   claims the next epoch, explicitly recovers the unresolved trigger, and
   continues to the same horizon. The final durable snapshot must equal a clean
   fenced control run.
2. **Open-transaction takeover serialization** — worker A pauses inside a real
   fenced transaction before COMMIT. Worker B starts a takeover concurrently
   and must remain blocked while A still holds the database transaction. After
   worker A is killed with `SIGKILL`, the database releases A's transaction;
   only then may worker B claim the next epoch, recover the unresolved trigger,
   and converge to the clean control snapshot.
3. **Zombie worker rejection** — worker A remains paused after its claim. Worker B
   claims the next epoch and completes the unresolved work. Worker A is then
   released and must fail with `StaleWriterError` on its next fenced
   transaction. It must not alter the state already committed by worker B.

This proves that process death does not strand durable ownership and that an
obsolete process cannot resume as a split-brain writer after a successor has
taken ownership.

## Level 4: PostgreSQL infrastructure failure

The `chaos postgres infrastructure gate` moves beyond individual session loss
and manipulates the database service and transport itself.

For each canonical example, the gate pauses a PostgreSQL-backed worker at a
representative transaction boundary and exercises both sides of COMMIT:

- **whole database outage** — the PostgreSQL service container is stopped while
  the worker is paused, the worker is released into a dead database, and the
  same container is restarted. A fresh `PostgresPersistence` then recovers the
  unresolved trigger and must converge to the clean control snapshot.
- **network interruption** — the worker connects through a disposable TCP proxy.
  The proxy is terminated while the connection is active, severing the socket
  without stopping PostgreSQL. The proxy is recreated on the same endpoint and
  a fresh persistence connection must recover to the same durable state.

Both tests run for failures immediately before and immediately after the chosen
COMMIT boundary. The outage test therefore also validates persistence across a
real PostgreSQL process restart, while the proxy test distinguishes transport
failure from server failure.

These tests establish runtime recovery after temporary PostgreSQL unavailability
and connection-path loss. They do not claim tolerance while the database remains
unavailable; SOSE resumes once authoritative storage becomes reachable again.

## Level 5: storage exhaustion and write failure

The `chaos storage-failure gate` targets the SQLite authoritative reference
adapter with storage conditions that are qualitatively different from process
death.

The gate covers:

- **disk-full semantics** using SQLite's real `SQLITE_FULL` path by constraining
  `max_page_count` and attempting a multi-page durable entity write. The
  transaction must roll back without advancing revision or committed state.
  After capacity is restored, the same write must succeed and survive reopen.
- **filesystem write denial** on POSIX by making both the database file and its
  directory read-only after a clean close. A fresh writer must fail without
  altering the previously committed state. Restoring permissions must allow the
  same database to reopen and continue.
- **live write denial** through SQLite `query_only` to exercise rollback while
  the process and connection remain alive, followed by a successful retry.

This gate establishes atomic rollback and resumability for the SQLite reference
store under storage exhaustion and write denial. It does not emulate physical
media corruption, torn sectors, or PostgreSQL server-side ENOSPC; those require
backend/host-specific fault facilities.
