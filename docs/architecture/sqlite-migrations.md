# SQLite persistence migrations

SQLitePersistence stores a schema version and a codec version separately.

These versions answer different compatibility questions:

- **schema version** — the SQLite table/layout contract;
- **codec version** — the tagged durable-payload representation.

A runtime may automatically migrate an older supported schema. It must never
silently open a database created by a newer schema or newer codec.

## Current versions

- schema: 2
- codec: 1

## Migration rule

Migrations are sequential and explicit.

```text
v1
 |
 | _migrate_v1_to_v2
 v
v2
```

Opening a database performs schema migration inside one SQLite
`BEGIN IMMEDIATE` transaction before durable state is decoded.

A failed migration rolls back rather than partially updating the database.

## v1 -> v2

v1 stored:

```text
singleton
schema_version
payload
```

v2 adds:

```text
codec_version
```

and advances the row's schema version to 2 while preserving the payload
unchanged.

This migration exists to establish a forward-compatible distinction between
storage layout and serialization format before either evolves independently.

## Compatibility guarantees

SQLitePersistence guarantees:

1. a supported old schema is upgraded before normal reads/writes;
2. durable payload content survives the migration unchanged;
3. reopening an already migrated database is idempotent;
4. a future schema is rejected without mutation;
5. a future codec is rejected before payload interpretation;
6. current schema/codec versions are observable through `schema_info()`.

## Adding the next migration

A future migration must:

1. increment `CURRENT_SCHEMA_VERSION`;
2. register exactly one migration from the previous version;
3. preserve or deliberately transform durable semantic truth;
4. include a legacy-file fixture proving upgrade behavior;
5. include an explicit rollback/failure test when the migration performs more
   than one data-changing step;
6. document any type/module-path compatibility implications.

Do not skip schema numbers and do not silently normalize unsupported old
databases.

## Codec migrations

The codec version is intentionally separate. If the tagged durable
representation changes incompatibly, schema migration alone is insufficient.

A future codec change should either:

- decode the previous codec and rewrite the payload in one migration; or
- reject it with explicit migration tooling.

The runtime must never guess which codec produced a payload.
