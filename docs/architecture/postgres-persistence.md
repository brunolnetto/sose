# PostgreSQL persistence

`PostgresPersistence` is SOSE's first remote operational persistence adapter.

Install the optional dependency:

```bash
pip install "sose[postgres]"
```

The adapter uses Psycopg 3 and the same backend-neutral record/delta mapping as
incremental SQLite.

## Configuration

Prefer an environment variable rather than embedding credentials in
`sose.toml`:

```toml
[persistence]
adapter = "postgres"
require = [
  "process_durable",
  "transactional_commits",
  "incremental_updates",
  "concurrent_writers",
  "remote",
]

[persistence.options]
dsn_env = "SOSE_DATABASE_URL"
namespace = "mro_production"
```

Then:

```bash
export SOSE_DATABASE_URL='postgresql://user:password@host/database'
sose doctor --config sose.toml
sose run --config sose.toml --trigger-id scheduler-run-001
```

`persistence.options.dsn` is also accepted for controlled environments, but
environment-based secrets are preferred.

## Storage model

Each namespace owns two tables:

```text
<namespace>_record_meta
<namespace>_record
```

The record table stores:

```text
collection
record_key
position
payload
```

Only dirty records are inserted/updated/deleted during a transaction.

The metadata table stores a monotonically increasing revision used to invalidate
per-connection caches.

## Concurrency contract

Independent PostgreSQL connections may execute overlapping transactions.

SOSE persists only dirty record identities, so two transactions touching
different records can both commit without replacing unrelated durable state.

The global revision row is incremented at commit and therefore briefly
serializes revision assignment, but record mutations themselves use PostgreSQL
row-level transactional behavior.

### Same-record conflicts

SOSE does not yet expose a generic optimistic-concurrency/CAS token in the
Persistence protocol.

Two writers that start from stale copies of the **same** semantic record are
therefore last-writer-wins.

This behavior is explicit rather than hidden. If real adoption requires
same-record conflict rejection, the correct next abstraction is a Persistence /
UnitOfWork compare-and-set contract shared by all capable adapters—not a
PostgreSQL-only exception.

## Capabilities

The adapter currently advertises:

- `process_durable`;
- `transactional_commits`;
- `incremental_updates`;
- `concurrent_writers`;
- `remote`.

It does **not** yet advertise:

- `analytical_reads`;
- `append_only`;
- `schema_migrations`.

A capability is only advertised after executable SOSE evidence exists.

## Executable evidence

CI starts a real PostgreSQL service and runs:

- PersistenceConformanceSuite unchanged;
- close/reopen + backend reconstruction;
- revision-based stale-cache refresh;
- concurrent disjoint-record writers.

PostgreSQL integration is therefore tested against the actual database protocol,
not a mocked connection.

## Namespace rule

Namespaces are SQL-safe identifiers up to 40 characters.

Namespaces allow independent SOSE jobs/tests to share one PostgreSQL database
without sharing durable semantic records.

A future production schema-management layer may replace table prefixes with
dedicated PostgreSQL schemas, but that is not required by the Persistence
contract.
