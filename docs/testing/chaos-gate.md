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

### Real process death

For every canonical example, a clean run first counts Engine OLTP transactions.
The gate then samples the first, middle, and final transaction boundaries and
starts the workload in a separate Python process. The child pauses immediately
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
