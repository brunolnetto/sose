# Persistence sinks

SOSE persistence adapters are required to preserve the same durable semantic
contract. Storage layout is an implementation choice; lifecycle meaning is not.

This document distinguishes three currently supported local persistence shapes.

## Snapshot SQLite

`SQLitePersistence` is the v0.8 correctness-first adapter.

Each committed transaction serializes the complete durable semantic state into
one tagged JSON payload stored in SQLite.

Strengths:

- simple transactional durability;
- straightforward schema/codec migration boundary;
- excellent reference implementation for process-boundary restart semantics.

Tradeoff:

- write amplification grows with total durable state because every semantic
  transaction rewrites the whole snapshot.

## Incremental SQLite

`SQLiteIncrementalPersistence` projects durable state into backend-neutral
records and writes only changed records per transaction.

The storage table is intentionally generic:

```text
(collection, record_key, position, payload)
```

This is **incremental**, but not yet domain-relational. It avoids coupling the
database schema to every SOSE runtime dataclass while allowing unchanged durable
records to remain untouched.

A transaction:

1. checks the durable database revision;
2. reloads/decodes records only when another connection has committed;
3. runs the same MemoryUnitOfWork validation semantics;
4. diffs pre/post semantic state;
5. applies only upserts/deletes;
6. atomically advances the durable revision when changes commit.

Normal reads use the same revision cache: one scalar revision check replaces a
full record reload when durable truth is unchanged.

The benchmark suite compares this adapter directly with snapshot SQLite.

## JSONL journal

`JSONLJournalPersistence` is an append-oriented, single-writer sink.

Each committed semantic transaction appends one fsynced JSON line containing
the same backend-neutral record delta used by Incremental SQLite.

Replay applies complete transactions in order.

A truncated final line is ignored as an incomplete crash fragment. Corruption
in the middle of the journal is an error.

This sink tests a different persistence shape:

```text
mutable relational state
        vs
append-only transaction history
```

It is useful for:

- audit/debug fixtures;
- offline replay experiments;
- portability testing of the record-delta contract.

It is **not** promoted as a concurrent multi-writer database.

## Shared record contract

`sose.persistence.records` maps the in-memory semantic state to keyed records
and computes deterministic record-level deltas.

The mapping is deliberately internal/advanced. It exists so persistence sinks
share semantics rather than each inventing their own interpretation of durable
truth.

## Adapter qualification

A persistence sink is supported only when it:

1. passes `PersistenceConformanceSuite` unchanged;
2. survives close/reopen plus backend reconstruction;
3. preserves deterministic durable ordering;
4. has explicit crash/transaction semantics;
5. participates in diagnostics;
6. participates in benchmark comparison;
7. documents concurrency and migration limitations.

## DuckDB

`DuckDBPersistence` is an optional embedded SQL sink using the same
backend-neutral record/delta mapping as Incremental SQLite.

Install it with:

```bash
pip install "sose[duckdb]"
```

or, from source:

```bash
pip install -e ".[duckdb]"
```

DuckDB is intentionally not imported by `sose.api`; optional database
dependencies remain explicit integration choices.

The adapter exists to test whether the record/delta mapping is portable across
independent SQL engines and to expose an analytical ecosystem without turning
DuckDB into a core dependency.

## PostgreSQL

PostgreSQL remains evidence-triggered. Incremental record persistence should be
measured first so a PostgreSQL adapter tests remote/concurrent database behavior,
not the already-known cost of full-state serialization.


## UnitOfWork dirty tracking

Record-oriented sinks no longer infer changed records by serializing and
diffing the complete durable state after every transaction.

`MemoryUnitOfWork` now records the semantic identities touched by its mutator
methods. Record sinks then compare/encode only those identities.

This changes the incremental cost model from approximately:

```text
transaction cost ~ total durable state
```

toward:

```text
transaction cost ~ touched durable records
```

The dirty set is internal persistence evidence, not a public domain API.
MemoryPersistence behavior is unchanged.

An idempotent save may still mark an identity as touched; the record encoder
compares the before/after Python values and emits no write if semantic truth did
not actually change.


## Capability contract

Persistence targets advertise operational capabilities through
`PersistenceCapabilities`.

The current built-in matrix is:

| Adapter | Process durable | Transactional commits | Incremental updates | Concurrent writers | Remote | Analytical reads | Append-only | Schema migrations |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| memory | no | yes | yes | no | no | no | no | no |
| sqlite | yes | yes | no | no | no | no | no | yes |
| sqlite_incremental | yes | yes | yes | no | no | no | no | yes |
| jsonl | yes | yes | yes | no | no | no | yes | no |
| duckdb | yes | yes | yes | no | no | yes | no | no |

`concurrent_writers=no` does not mean the adapter cannot be opened from
multiple processes/connections. It means SOSE does not claim simultaneous
multi-writer execution as a supported capability. Incremental SQLite currently
serializes writers through SQLite locking.

Declarative jobs may require capabilities:

```toml
[persistence]
adapter = "sqlite_incremental"
require = [
  "process_durable",
  "transactional_commits",
  "incremental_updates",
]
```

The job is rejected before execution when the selected adapter does not satisfy
the requirements.

This prevents a future warehouse adapter from silently weakening a job's
operational assumptions.

### Future adapters

A PostgreSQL adapter would reasonably be expected to advertise
`process_durable`, `transactional_commits`, `incremental_updates`,
`concurrent_writers`, and `remote` once those semantics are actually proven.

Databricks or Snowflake may advertise `remote` and `analytical_reads`, but
must only advertise transactional/concurrency capabilities that their SOSE
adapter has executable evidence for.

Capabilities describe tested SOSE adapter semantics, not generic marketing
claims about the underlying database product.
