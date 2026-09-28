# Declarative recurring jobs

SOSE jobs can be configured from a small `sose.toml` file.

The file selects:

- the executable domain;
- domain-specific parameter overrides;
- the durable persistence adapter;
- the runtime backend;
- the persistent job identity.

Example:

```toml
[domain]
name = "mro"

[domain.parameters]
quantity = 2.0
technician_capacity = 2
maintenance_bay_capacity = 2
spare_part_store_capacity = 20
release_delay = "PT1H"
tick_step = "PT30M"
random_seed = 77

[persistence]
adapter = "sqlite_incremental"

[persistence.options]
path = "state/mro.sqlite3"

[runtime]
backend = "simpy"

[job]
id = "mro-recurring"
```

Load and run exactly one tick:

```python
from sose.api import build_job_from_file

job = build_job_from_file("sose.toml")
result = job.run_tick(trigger_id="scheduler-run-2026-09-28T18:00")
```

A scheduler should invoke the same job again for the next tick.

The job does not require one long-lived Python process. Its durable
`SimulationJobState`, domain state, ScheduledWork, and SimulationPosition are
stored in the selected persistence adapter.

## Configuration ownership

The `[domain.parameters]` table is validated by the selected domain's own
`DomainConfig` model.

There is no giant global parameter schema.

For example, MRO owns parameters such as:

- quantity;
- technician capacity;
- maintenance-bay capacity;
- spare-parts Store capacity;
- release delay.

Common orchestration values such as `start_at`, `tick_step`, and
`random_seed` are inherited from the common DomainConfig base.

## Configuration changes after initialization

The configuration stored inside the durable job state is authoritative.

Editing `sose.toml` does **not** silently mutate an existing job.

Use:

```python
job.update_config({
    "tick_step": "PT15M",
    "technician_capacity": 3,
})
```

This creates a new durable `config_revision`.

That separation prevents accidental behavior changes merely because a deployment
mounted a different configuration file.

## Persistence adapters

Built-in names are:

```text
memory
sqlite
sqlite_incremental
jsonl
duckdb
```

Paths inside `[persistence.options]` are resolved relative to the directory
containing `sose.toml`.

DuckDB remains an optional dependency.

Future adapters such as PostgreSQL, Databricks, and Snowflake should register
through the same PersistenceRegistry rather than introduce special job APIs.

## Recurring execution model

The recommended operational model is:

```text
scheduler / orchestrator
        |
        v
load sose.toml
        |
        v
open durable store
        |
        v
resume SimulationJob
        |
        v
run_tick()
        |
        v
persist checkpoint
        |
        v
process exits
```

The next invocation reconstructs the runtime from the committed durable
position.

This works naturally with cron, Airflow, Databricks Jobs, Kubernetes CronJobs,
GitHub Actions, or another external scheduler.

## Trigger identity

Production schedulers should pass a stable external run identifier:

```python
job.run_tick(trigger_id=external_run_id)
```

Repeating a completed trigger is idempotent.

An unresolved trigger cannot be silently replaced by another trigger. Recovery
of a failed/crashed trigger is explicit:

```python
job.run_tick(
    trigger_id=external_run_id,
    recover=True,
)
```

This prevents two scheduler invocations from unintentionally advancing the same
job twice.
