# Persistence conformance contract

Every SOSE persistence adapter must satisfy the same behavioral contract as the
semantic runtime. The protocol surface alone is insufficient: recovery depends
on transactional and ordering guarantees.

## Required guarantees

### Atomic transactions

Writes inside one transaction become visible together or not at all.

```text
begin
  save A
  save B
commit
```

A raised exception must leave neither A nor B visible.

### Isolation from mutable caller objects

Persistence must copy or otherwise isolate mutable payloads. Mutating a value
returned by a read must never mutate durable state implicitly.

### Deterministic ordered readers

Readers for sequence-bearing durable records must return deterministic semantic
order, not storage-engine order. At minimum this applies to demands, pending
operations, and terminal results whose sequence participates in replay.

### Idempotent identical writes

Saving the exact same durable definition/state twice may be accepted as an
idempotent operation. Saving a conflicting object under the same semantic
identity must fail.

### Terminal identity

A terminal result reserves the request identity that produced it. Persistence
must not permit that request to be reopened as pending after completion.

### Atomic state transitions

Semantic transitions such as:

```text
pending request
→ terminal result
```

must be representable in one transaction.

## Reusable test suite

`tests/support/persistence_conformance.py` contains
`PersistenceConformanceSuite`.

A new adapter should expose only a factory:

```python
class TestPostgresPersistenceConformance(PersistenceConformanceSuite):
    def make_persistence(self):
        return PostgresPersistence(...)
```

The same suite should run unchanged against every supported persistence backend.


## SQLite reference adapter

`SQLitePersistence` is the first external transactional adapter.

Its v0.8 implementation intentionally stores one tagged-JSON semantic snapshot
inside SQLite rather than prematurely normalizing every durable record into a
database-specific schema. That design has two purposes:

1. prove the Persistence contract survives a real serialization/process boundary;
2. preserve exactly the same validation semantics as MemoryPersistence.

The adapter therefore optimizes for correctness and portability before query
performance. A future normalized SQLite/PostgreSQL schema may replace this
storage layout without changing the public Persistence protocol.

### Serialization contract

SQLitePersistence does not use pickle.

The tagged JSON codec preserves SOSE dataclasses, datetime/date, enums, UUIDs,
bytes, tuples, sets/frozensets, mappings, and exact float bit patterns. Values
outside the supported durable codec fail explicitly instead of falling back to
`repr()` or `str()`.

The database is treated as trusted application state. The codec resolves durable
dataclass/enum types by their Python module and qualified name, so moving those
types is a persistence compatibility concern and must be documented during the
pre-1.0 line.

### Reopen evidence

In addition to the reusable conformance suite, a Reference-level restart test:

1. persists an in-progress ITSM incident and SLA boundary;
2. closes the SQLite connection;
3. creates a fresh SQLitePersistence from the same file;
4. rebuilds a fresh ephemeral backend;
5. executes the durable SLA boundary and completes reconciliation.

This is intentionally stronger than rebuilding against the same in-memory object.


## Schema evolution

SQLite schema and codec evolution are versioned separately. Supported older
schemas migrate transactionally on open; newer schemas/codecs are rejected
without mutation.

See [SQLite persistence migrations](sqlite-migrations.md).
