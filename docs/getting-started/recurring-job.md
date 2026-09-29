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
ticks_per_trigger = 1
max_ticks_per_trigger = 100
```

Load and run exactly one tick:

```python
from sose.api import build_job_from_file

job = build_job_from_file("sose.toml")
result = job.run_tick(trigger_id="manual-tick-001")
```

For recurring orchestration, prefer a durable trigger batch:

```python
result = job.run_trigger(
    trigger_id="airflow-run-2026-09-28T18:00",
)
```

The batch advances `ticks_per_trigger` logical ticks from `sose.toml`.
`max_ticks_per_trigger` is an explicit safety bound. A caller may override
the batch size for one trigger with `ticks=N`, but never above that bound.

The same operation is available from the CLI:

```bash
sose trigger \
  --config sose.toml \
  --trigger-id airflow-run-2026-09-28T18:00
```

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

For declarative deployments, edit `sose.toml` and explicitly apply it:

```bash
sose apply --config sose.toml
```

Applying the same desired configuration twice is idempotent and does not create
another revision.

Programmatic callers can still use:

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


## Doctor

Before advancing a production job, inspect the declarative configuration and
durable checkpoint without mutating either:

```bash
sose doctor --config sose.toml
```

The command checks:

- domain configuration validity;
- persistence adapter resolution;
- durable job/domain identity;
- declarative-vs-durable config drift;
- job status and unresolved trigger ownership;
- SimulationJobState vs SimulationPosition tick/time coherence;
- runtime diagnostic issue codes.

A fresh valid configuration with no initialized job is healthy.

If `sose.toml` differs from the durable configuration revision, doctor reports
`job.config_drift`. Apply the change explicitly:

```bash
sose apply --config sose.toml
```

Then rerun doctor.

A non-healthy doctor report exits with status 1. Configuration/loading failures
still use the normal CLI error status 2.

## Minimal operational loop

SOSE itself intentionally advances **one durable tick per invocation**:

```text
external scheduler
      |
      v
sose doctor
      |
      v
sose run --trigger-id <stable scheduler run id>
      |
      v
checkpoint and process exit
```

This makes scheduler ownership explicit. Cron, Airflow, Databricks Jobs,
Kubernetes CronJobs, or another orchestrator owns wall-clock recurrence; SOSE
owns logical tick progression and idempotent recovery.


## Persistence capability requirements

A job can make its storage assumptions explicit:

```toml
[persistence]
adapter = "sqlite_incremental"
require = [
  "process_durable",
  "transactional_commits",
  "incremental_updates",
]
```

`sose validate`, `sose doctor`, and job construction all enforce these
requirements.

List the tested capability surface of the installed adapters with:

```bash
sose persistence
```

This becomes especially important when selecting remote warehouses. A job that
requires concurrent writers must not silently run on a target whose adapter only
supports serialized writers.
