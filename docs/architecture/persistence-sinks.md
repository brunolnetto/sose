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

1. loads the current record set;
2. runs the same MemoryUnitOfWork validation semantics;
3. diffs pre/post semantic state;
4. applies only upserts/deletes;
5. commits atomically.

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

## Next sink: DuckDB

DuckDB is a good next optional adapter because it exercises a separate embedded
SQL implementation and analytical ecosystem.

It should reuse the same record/delta mapping and remain an optional dependency.
It should not be added to core dependencies merely to increase the sink count.

## PostgreSQL

PostgreSQL remains evidence-triggered. Incremental record persistence should be
measured first so a PostgreSQL adapter tests remote/concurrent database behavior,
not the already-known cost of full-state serialization.
