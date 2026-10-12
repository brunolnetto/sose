# OD-5a PostgreSQL causal-recovery falsifiers (PRD/TRD/ADR)

Parent: #477. Predecessors: #479 (physical event ingress), #482 (terminal
observation and certificate), #484 (SQLite one-step reconciler and OS kills).
This is a **non-confirmatory** incremental promotion test for the bounded
reconciler, not admission to the 17,920-run OD-5a DOE.

## PRD — what needs falsification

A real PostgreSQL adapter must reconstruct an independently persisted
OD-5a physical-event chain after process death, without fabricating a second
physical execution or conflating ACK, staged Command, observation and terminal
certificate. Distinct organizations must retain their own physical source
DAG and state when workers interleave on the same database namespace.

The previously existing PostgreSQL observation test was conditional on
`SOSE_TEST_POSTGRES_DSN` and was not explicitly listed in the PostgreSQL CI
integration job. This PR makes that check mandatory and adds real-process
reconciliation falsifiers.

## TRD — evidence gates

The explicit PostgreSQL CI step runs:

- `test_od5a_observation_postgres.py`: complete durable state conforming to
  SQLite over the event → publication → ACK → observation → certificate chain.
- `test_od5a_reconciler_postgres.py`: all five interventions, fresh SQL
  connections per phase, kill by `os._exit` before and after each of the five
  durable boundary operations, two independent processes racing for a fenced
  claim with PostgreSQL \`pg_try_advisory_xact_lock\` evidence of a waiting advisory
  lock (with both SQL connections already bootstrapped before contention), stale
  claim rejection after an expired lease is taken over by a new
  owner at a strictly higher fencing epoch, and two organizations progressing
  in the same namespace after an actual process death.

The cross-run oracle compares event history and typed causes, source entities,
physical resource definitions/reservations, all persisted boundary messages/deliveries/consumptions (including
orphan detection), claim epochs and owners, every terminal observation and
pending Command and immutable effect certificates. The asserted reference is the
uninterrupted run with identical exogenous input, worker identity and
clock. Tests reject duplicate or missing certificates.

## ADR — limits / stop rule

- Reuse the existing PostgreSQL transaction and boundary locking semantics.
  Do not add a global organization clock or persist any SimPy object.
- The OS-kill matrix proves committed PostgreSQL state survives **client
  process** death with the SQL session still open and fresh-connection recovery; it does **not** simulate
  `pg_ctl stop`, PostgreSQL WAL failure, an actual network partition, or
  unknown-commit acknowledgment at the server boundary.
- Concurrent claim tests detect double ownership. They do not yet prove
  parallel throughput or absence of all namespace-level lock contention.
- The bounded reconciler still does not execute the real recurrent
  SimPy-backed scheduler. Cross-adapter SimPy vs authoritative SQLite vs
  recovered PostgreSQL full-DAG equivalence is a separate #477 gate.
- An explicit `SOSE_TEST_POSTGRES_DSN` in CI is mandatory. The historical
  Manufacturing/O2C/MRO v1 scientific freezes must remain untouched.
- The independent execution freeze remains `NOT_ADMITTED`; never launch
  OD-5a confirmatory DOE from this operational check.

The adversarial claim test pre-opens both PostgreSQL connections before the
transaction barrier: schema bootstrap takes an exclusive namespace advisory
lock, which can legitimately block a just-starting process while another
transaction owns a shared namespace writer lock. Mixing that bootstrap wait
with live-delivery contention produces a false proof of claim fencing.

For the contested claim, both workers publish their real `pg_backend_pid()`.
The observer checks A holds the namespace advisory lock AND that B has a
non-granted `pg_locks` entry matching A's `database/classid/objid/objsubid`.
The test does not count another connection's unrelated advisory wait.
