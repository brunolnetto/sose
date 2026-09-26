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
