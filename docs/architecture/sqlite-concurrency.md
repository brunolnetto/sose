# Incremental SQLite concurrency semantics

`SQLiteIncrementalPersistence` supports multiple adapter instances against the
same SQLite file, but its guarantees are deliberately narrower than a
networked multi-writer database contract.

## Read visibility

Each adapter keeps a cached semantic state plus the durable global revision.

A read performs a cheap revision check:

```text
cached revision == database revision
    -> reuse cached state

cached revision != database revision
    -> reload durable records
    -> replace cached semantic state
```

This gives deterministic stale-read detection between independent connections
after another connection commits.

## Write serialization

Transactions use `BEGIN IMMEDIATE`.

Therefore only one SQLite writer owns the write transaction at a time.

A competing writer:

- waits according to SQLite busy-timeout behavior;
- succeeds after the first writer commits/releases; or
- receives an explicit `sqlite3.OperationalError: database is locked` when its
  timeout expires.

SOSE does not silently retry a timed-out write inside the persistence adapter.
Retry policy belongs to the caller/application because replay safety depends on
the semantic operation being retried.

## Latest-state semantic evaluation

The important runtime contract is:

> semantic operations must load authoritative durable state inside the UnitOfWork
> after the writer lock is acquired.

SOSE Engine dispatch already follows this rule.

When two independent engines prepare conflicting commands against the same
entity:

1. writer A acquires the transaction;
2. writer A reloads and commits the transition;
3. writer B later acquires the transaction;
4. writer B refreshes durable state;
5. writer B evaluates its command against the newly committed entity state.

If B's command is no longer legal, the StateChart rejects it. It does not
overwrite A's committed state.

## Detached stale entities

The current Persistence contract does **not** attach a compare-and-swap token to
arbitrary detached Entity objects.

Therefore this pattern is not multi-writer safe:

```python
stale = persistence.entity("order", order_id)

# another connection commits a newer value

with persistence.transaction() as uow:
    uow.save_entity(stale)
```

A later explicit `save_entity(stale)` is currently last-writer-wins.

`Entity.version` is not redefined as a persistence CAS token. It already has
domain/state-transition meaning.

For multi-writer applications, mutate authoritative entities loaded from the
active UnitOfWork instead:

```python
with persistence.transaction() as uow:
    current = uow.get_entity("order", order_id)
    # validate + mutate current
    uow.save_entity(current)
```

## Tested concurrency cases

The executable suite covers:

- independent connections observing commits;
- alternating writers and monotonic database revisions;
- stale cached readers refreshing on the next read;
- explicit lock contention / timeout;
- adapter recovery after a lock failure;
- conflicting Engine commands evaluating latest committed state;
- the documented detached-object last-writer-wins limitation.

## PostgreSQL implication

This evidence sharpens, but does not yet automatically trigger, PostgreSQL.

SQLite is sufficient when:

- writes can serialize;
- a single-host file is acceptable;
- application logic follows UnitOfWork read-modify-write discipline;
- explicit lock timeout/retry behavior is acceptable.

PostgreSQL becomes materially more useful when adopters require:

- sustained concurrent writers rather than serialized writes;
- remote/networked durability;
- row-level concurrency control;
- database-managed HA/replication/backup;
- explicit optimistic/pessimistic concurrency tokens across services.

A future PostgreSQL adapter should not silently redefine detached Entity save
semantics. If compare-and-swap becomes a library requirement, that contract
belongs in Persistence/UnitOfWork first and must also be implementable by
SQLite.
