# Analytical sinks and durable outbox

SOSE separates two responsibilities:

```text
operational persistence
    = authoritative durable simulation truth

analytical sink
    = downstream copy of committed events for analytics/replay/integration
```

A warehouse sink never owns simulation semantics.

## Declarative configuration

```toml
[persistence]
adapter = "postgres"

[persistence.options]
dsn_env = "SOSE_DATABASE_URL"
namespace = "mro_daily"

[[sinks]]
name = "lakehouse"
adapter = "jsonl"

[sinks.options]
path = "analytics/events.jsonl"
```

Future Databricks and Snowflake adapters plug into the same `SinkRegistry`.

## Delivery contract

Each sink receives deterministic `AnalyticalBatch` objects containing committed
DomainEvents and a stable `batch_id`.

The durable operational store also persists:

- pending/completed `SinkDelivery` records;
- one `SinkCheckpoint` per `job_id + sink_name`.

The checkpoint is an event-log offset.

## Failure semantics

The order is intentionally:

```text
commit simulation tick
        |
        v
prepare durable delivery
        |
        v
publish to sink
        |
        v
ack delivery + advance sink checkpoint
```

If the sink is unavailable:

- the simulation tick remains committed;
- the delivery remains pending;
- the checkpoint does not advance;
- the next recurring invocation retries it.

If publish succeeds but the process dies before acknowledgement, retry uses the
same deterministic batch id. Concrete sinks must therefore be idempotent by
`batch_id`.

The reference `JSONLAnalyticalSink` demonstrates this contract by avoiding a
duplicate append when the same batch is retried.

## Observability

`Engine.diagnostics()` reports:

- pending analytical deliveries;
- failed analytical deliveries;
- `sink.delivery_failed` issues.

`sose doctor` reports `sink.delivery_pending` for the selected job.

## Why sinks are separate from persistence adapters

PostgreSQL and incremental SQLite are good examples of authoritative operational
stores.

Databricks, Snowflake, object storage, and event streams often make better
analytical/replication targets than transaction coordinators for SOSE semantic
truth.

The separation lets one job use, for example:

```text
MRO domain
   |
   +-- PostgreSQL authoritative state
   |
   +-- Databricks events
   +-- Snowflake events
   +-- JSONL audit archive
```

without changing domain code or the recurring tick model.

## Current scope

The first sink mode exports committed DomainEvents.

Snapshot/entity projection modes should be added as separate explicit contracts,
not inferred from the event sink.

Built-in analytical adapters are:

- `jsonl`;
- `databricks` (optional extra `sose[databricks]`);
- `snowflake` (optional extra `sose[snowflake]`).

## Databricks

The Databricks adapter uses the Databricks SQL Connector and writes to Delta
tables through idempotent `MERGE` operations.

```toml
[[sinks]]
name = "lakehouse"
adapter = "databricks"

[sinks.options]
server_hostname_env = "DATABRICKS_SERVER_HOSTNAME"
http_path_env = "DATABRICKS_HTTP_PATH"
access_token_env = "DATABRICKS_TOKEN"
events_table = "main.sose.events"
batches_table = "main.sose.batches"
```

Install with:

```bash
pip install "sose[databricks]"
```

The SQL Connector uses positional native parameters; SOSE validates table
identifiers separately because SQL identifiers cannot be value-bound.

## Snowflake

The Snowflake sink uses the GA 4.x Python connector line.

```toml
[[sinks]]
name = "warehouse"
adapter = "snowflake"

[sinks.options]
account_env = "SNOWFLAKE_ACCOUNT"
user_env = "SNOWFLAKE_USER"
password_env = "SNOWFLAKE_PASSWORD"
warehouse_env = "SNOWFLAKE_WAREHOUSE"
database_env = "SNOWFLAKE_DATABASE"
schema_env = "SNOWFLAKE_SCHEMA"
events_table = "SOSE_EVENTS"
batches_table = "SOSE_BATCHES"
```

Alternatively, provide `connection_name` to use a Snowflake connector connection
definition.

Install with:

```bash
pip install "sose[snowflake]"
```

## Warehouse table contract

Both SQL warehouse sinks materialize:

```text
sose_events
  event_id
  batch_id
  job_id
  domain_name
  config_revision
  logical_tick
  logical_time
  event_name
  entity_type
  entity_id
  occurred_at
  event_tick
  payload_json
  causation_id
  correlation_id

sose_batches
  batch_id
  job_id
  domain_name
  config_revision
  logical_tick
  logical_time
  event_count
```

Events are merged by `event_id`. The batch marker is merged only after event
writes. A partial failure can therefore replay the same deterministic batch
without duplicating events.

These sinks remain downstream analytical copies; neither Databricks nor
Snowflake becomes authoritative operational truth merely by being configured as
a sink.
