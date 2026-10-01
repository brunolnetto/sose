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
