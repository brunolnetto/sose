# Authoritative persistence qualification

SOSE distinguishes what an adapter **claims** from what executable evidence proves.

## Promotion tiers

- **ANALYTICAL** — receives events, snapshots, occurrences, or metrics; it does not decide operational truth.
- **DURABLE** — can persist and reconstruct operational state within a documented envelope.
- **AUTHORITATIVE** — has passed the authoritative conformance suite for its documented operational envelope.

An adapter MUST NOT infer authoritative status merely because its capability flags are true.

## Minimum semantic guarantees

Authoritative qualification requires executable evidence for:

1. atomic semantic UnitOfWork transitions;
2. authoritative read-after-commit visibility;
3. conflict-safe conditional ownership;
4. fencing of stale owners after lease expiry or reassignment;
5. full fresh-process reconstruction;
6. deterministic semantic ordering and continuation;
7. monotonic/immutable terminal identities;
8. safe schema and codec migration, including rejection of unsupported future versions.

Qualification is scoped by a `ConcurrencyEnvelope`. A single-writer local store may be authoritative within that envelope without claiming distributed multi-writer authority.

## Reference oracle

The authoritative suite compares a faulted run against an uninterrupted reference run:

```text
reference = continuous execution

candidate =
  execute
  -> hard kill
  -> reopen in a fresh process
  -> reconstruct
  -> race permitted writers
  -> inject commit failures
  -> migrate supported schema
  -> reopen
  -> continue
```

The assertion is semantic rather than byte-for-byte:

```text
semantic_projection(candidate) == semantic_projection(reference)
```

Physical transaction IDs, connection IDs, storage versions, lease epochs, and non-semantic row order are excluded from the projection.

## Promotion rule

```text
capability declaration + executable conformance evidence = qualification
```

Warehouse product categories are irrelevant. PostgreSQL, SQLite, Delta, Snowflake, Iceberg, or another technology may qualify only for the guarantees and operational envelope its adapter demonstrates.
