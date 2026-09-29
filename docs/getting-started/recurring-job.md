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

Inspect the available parameters, defaults, types, enums, and constraints with:

```bash
sose domains --name mro
```

The same metadata is available through
`DomainDefinition.describe_config()`, including referenced `$defs` for enums
and nested models.

Every parameter also reports `mutability`:

- `runtime` can be changed with `sose apply` between completed triggers;
- `bootstrap` is fixed after initialization because it already contributed to
  durable domain state.

Generated `sose.toml` files include this mutability directly in comments.

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

SOSE exposes two explicit operational primitives:

```text
sose run
    -> exactly one durable tick

sose trigger --trigger-id <stable external id>
    -> bounded durable batch of ticks
    -> size = ticks_per_trigger
```

A recurring trigger has its own durable ownership and progress. If a process
fails after completing only part of the batch, retry the **same** trigger id
with `--recover`; SOSE resumes from the first unfinished child tick.

Completed trigger ids remain durably recorded, so replaying an older scheduler
run after later runs have completed does not advance logical time again.

Cron, Airflow, Databricks Jobs, Kubernetes CronJobs, or another orchestrator
owns wall-clock recurrence; SOSE owns logical tick progression, bounded catch-up,
checkpointing, and idempotent recovery.


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


### PostgreSQL

Generate a PostgreSQL-backed job:

```bash
sose init \
  --domain mro \
  --persistence postgres \
  --job-id mro-production
```

The scaffold uses:

```toml
[persistence]
adapter = "postgres"

[persistence.options]
dsn_env = "SOSE_DATABASE_URL"
namespace = "mro_production"
```

Set the connection secret outside the file:

```bash
export SOSE_DATABASE_URL='postgresql://user:password@host/database'
```

PostgreSQL is an optional extra:

```bash
pip install "sose[postgres]"
```


## Inspect and edit domain parameters

The declarative file remains the source of desired configuration.

Use:

```bash
sose config show --config sose.toml
```

to see the effective values plus each parameter's `runtime` or `bootstrap`
mutability.

Edit one validated parameter without hand-editing TOML:

```bash
sose config set auto_complete false --config sose.toml
sose config set random_seed 99 --config sose.toml
```

Values are interpreted as JSON when possible, otherwise as strings. This makes
booleans and numbers ergonomic while still allowing Pydantic-supported strings
such as ISO-8601 durations.

`config set` changes only the declarative file. It never mutates a durable job
implicitly.

For an initialized job:

```text
sose config set ...
        |
        v
sose.toml desired state
        |
        | explicit
        v
sose apply
        |
        v
durable config revision
        |
        v
next trigger / reconcile_tick
```

A bootstrap-only field may be edited in the file, but `sose apply` rejects
that change after initialization. Runtime fields advance `config_revision`
and affect the next completed-trigger boundary.


## Scheduler-friendly trigger identity

External schedulers should prefer passing the scheduled occurrence timestamp
instead of inventing their own trigger id.

For example:

```bash
sose trigger \
  --config sose.toml \
  --scheduled-for 2026-09-29T12:00:00Z
```

SOSE derives:

```text
<job-id>:scheduled:<UTC scheduled instant>
```

The identity is normalized to UTC, so these represent the same occurrence:

```text
2026-09-29T12:00:00Z
2026-09-29T09:00:00-03:00
```

Retrying the same scheduled occurrence is therefore idempotent. A different
scheduled occurrence receives a different durable batch identity.

This is the preferred integration contract for cron, Airflow, Databricks Jobs,
Kubernetes CronJob, Azure schedulers, and similar systems:

```text
scheduler occurrence timestamp
            |
            v
SOSE deterministic trigger identity
            |
            v
durable run_trigger ownership
            |
            v
one bounded tick batch
```

Timezone-naive timestamps are rejected because they cannot provide a globally
stable occurrence identity.
