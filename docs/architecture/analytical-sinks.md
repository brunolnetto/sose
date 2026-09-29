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

The current built-in analytical adapter is:

- `jsonl`.

Databricks and Snowflake should implement the same batch/idempotency contract.
