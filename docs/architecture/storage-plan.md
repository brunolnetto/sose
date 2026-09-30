# Storage plan

A SOSE job has two different storage roles:

```text
parameterized domain
        |
        v
durable SimulationJob
        |
        +-- authoritative persistence
        |
        +-- analytical sink(s)
```

The distinction is semantic, not branding.

## Authoritative persistence

The authoritative store owns SOSE durable operational truth:

- entities;
- commands;
- ScheduledWork;
- events;
- resources/stores/containers;
- SimulationPosition;
- durable job checkpoint;
- sink outbox/checkpoints.

It must implement the shared Persistence contract.

Examples:

- `sqlite_incremental`;
- `sqlite`;
- `postgres`;
- `duckdb`;
- `jsonl`;
- `memory` for ephemeral/test use.

The selected adapter declares capabilities such as:

- `process_durable`;
- `transactional_commits`;
- `incremental_updates`;
- `concurrent_writers`;
- `remote`;
- `analytical_reads`;
- `append_only`;
- `schema_migrations`.

A job can require capabilities declaratively:

```toml
[persistence]
adapter = "postgres"
require = [
  "process_durable",
  "transactional_commits",
  "concurrent_writers",
  "remote",
]
```

The storage plan fails before job construction when the selected adapter cannot
meet a required capability.

## Analytical sinks

Analytical sinks receive idempotent event batches through the durable sink
outbox. They do not own simulation truth.

Examples:

```toml
[[sinks]]
name = "lakehouse"
adapter = "databricks"

[sinks.options]
events_table = "main.operations.sose_events"
batches_table = "main.operations.sose_batches"

[[sinks]]
name = "warehouse"
adapter = "snowflake"

[sinks.options]
events_table = "SOSE_EVENTS"
batches_table = "SOSE_BATCHES"
```

The job may continue to use PostgreSQL or SQLite as authoritative persistence
while publishing committed analytical evidence to Databricks/Snowflake.

## Unified inspection

Use:

```bash
sose storage --config sose.toml
```

Example result:

```json
{
  "authoritative": {
    "role": "authoritative",
    "adapter": "postgres",
    "capabilities": [
      "process_durable",
      "transactional_commits",
      "incremental_updates",
      "concurrent_writers",
      "remote"
    ],
    "durable_recurring_ready": true
  },
  "analytical": [
    {
      "role": "analytical",
      "name": "lakehouse",
      "adapter": "databricks"
    }
  ],
  "issues": []
}
```

This command is read-only: it validates the storage plan without opening the
authoritative database or sink connections.

## Why Databricks and Snowflake are not currently authoritative

SOSE already supports Databricks and Snowflake as analytical sinks. They are not
currently registered as Persistence adapters because that would require proving
the same durable job/transaction/restart contract used by SQLite/PostgreSQL.

This is intentionally capability-driven.

A future Databricks/Snowflake authoritative adapter must qualify through:

1. PersistenceConformanceSuite;
2. process/restart recovery;
3. durable job checkpoint roundtrip;
4. concurrent/retry semantics;
5. schema/version evolution;
6. benchmark and diagnostics coverage.

Until then:

```text
Postgres / SQLite
    = authoritative operational truth

Databricks / Snowflake
    = analytical publication destinations
```

That separation lets the user choose a warehouse-oriented output without
weakening the durable runtime contract.

## Product model

The product-facing model is therefore:

```text
DomainConfig
   +
SimulationJob
   +
StoragePlan
   +
external recurring trigger
```

with `sose.toml` as the declarative composition point.
