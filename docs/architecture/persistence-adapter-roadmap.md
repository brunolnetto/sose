# Persistence adapter roadmap

## Current state

SOSE currently has:

- MemoryPersistence — deterministic in-process reference implementation;
- SQLitePersistence — external transactional/file-backed adapter with tagged
  durable serialization and schema migrations.

The public Persistence protocol and reusable conformance suite are the contract;
database-specific storage layout is not.

## Initial measured evidence

The first reduced CI benchmark run after the benchmark harness was introduced
used Python 3.14.7 on a 4-vCPU Azure-hosted Linux runner.

It observed, in that **single quick run**:

- Memory scheduled batch: about 329 items/s;
- SQLite scheduled batch: about 82 items/s;
- SQLite reopen/rebuild: about 110 items/s;
- diagnostics collection: about 521 items/s;
- Resource contention: about 944 items/s;
- Priority Store selection: about 425 items/s.

These values are **not a baseline, SLA, or regression threshold**. The quick
suite uses one repetition and different workload cardinalities.

The useful signal is qualitative: SQLite's correctness-first full semantic
snapshot rewrite is already visible as a meaningful cost relative to
MemoryPersistence.

## Implication

Adding PostgreSQL while retaining the same "serialize the whole durable state on
every transaction" architecture would test network/database connectivity but
would not address the dominant write-amplification question.

Therefore the next persistence decision should be evidence-driven:

### If throughput/write amplification becomes the problem

Prototype normalized/incremental durable storage behind the existing Persistence
contract, preferably in SQLite first because it preserves simple deterministic
test infrastructure.

The experiment should measure:

- transaction size;
- rows/bytes rewritten per semantic operation;
- scheduled/resource/store workload throughput;
- reopen/recovery time;
- migration complexity.

### If concurrency/remote durability becomes the problem

Add PostgreSQL as a second external adapter.

The adapter should reuse:

- PersistenceConformanceSuite;
- durable codec rules where payload encoding remains necessary;
- schema migration policy;
- restart/reference tests;
- diagnostics and benchmark workloads.

It should not invent domain semantics or a PostgreSQL-specific runtime contract.

## PostgreSQL trigger

Proceed when a concrete requirement appears for one or more of:

- multi-process writers/readers;
- remote database service;
- stronger database-managed HA/backup;
- explicit transaction/concurrency behavior SQLite cannot satisfy;
- ecosystem integration requiring PostgreSQL.

Until then, PostgreSQL remains a planned adapter, not a maturity checkbox.

## Adapter promotion rule

A new persistence adapter is "supported" only when:

1. the reusable conformance suite passes unchanged;
2. process-boundary reopen/recovery is demonstrated;
3. schema/version evolution is explicit;
4. diagnostics can inspect the adapter's durable truth;
5. benchmark workloads can measure it;
6. compatibility limitations are documented.


## Implemented incremental experiment

`SQLiteIncrementalPersistence` and `JSONLJournalPersistence` now share a
backend-neutral record/delta projection. The benchmark suite compares snapshot
SQLite, incremental SQLite, and append-only JSONL under the same scheduled and
reopen workloads.

The next decision should use these measurements before selecting PostgreSQL.
See [Persistence sinks](persistence-sinks.md).


## Concurrency evidence

Incremental SQLite now has executable multi-connection semantics for read
visibility, alternating writers, write-lock contention, and conflicting Engine commands that become semantic no-ops after the first commit.

The key limitation is explicit: detached stale Entity objects remain
last-writer-wins because the Persistence contract does not define compare-and-swap
metadata. Core Engine operations are safer because they reload authoritative
state inside the UnitOfWork after acquiring the writer transaction.

See [Incremental SQLite concurrency semantics](sqlite-concurrency.md).

This is evidence for a future PostgreSQL adapter only when real adoption needs
sustained concurrent writers, remote durability, row-level concurrency, or HA.
It is not evidence that PostgreSQL should redefine the Persistence semantic
contract.
