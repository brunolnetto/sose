# SOSE command-line interface

The `sose` command is a thin wrapper over the public declarative job APIs.

It does not implement a second orchestration model.

## Create a configuration

Generate an editable `sose.toml` directly from a domain's validated defaults:

```bash
sose init --domain mro
```

Choose job identity and persistence location explicitly when needed:

```bash
sose init \
  --domain mro \
  --job-id plant-maintenance \
  --persistence sqlite_incremental \
  --persistence-path state/mro.sqlite3
```

The generated `[domain.parameters]` table comes from the selected
`DomainConfig` model. SOSE does not maintain a separate handwritten template
schema.

Existing files are not overwritten unless `--force` is supplied.

## Validate configuration

```bash
sose validate --config sose.toml
```

This validates:

- TOML structure;
- domain existence;
- domain-specific parameter schema;
- persistence adapter name;
- runtime backend name.

## Run one durable tick

```bash
sose run --config sose.toml --trigger-id scheduler-2026-09-28T18:00
```

Each invocation advances exactly one logical tick and exits.

A recurring scheduler should invoke the command again with a new stable trigger
identifier.

To explicitly recover the same failed/crashed trigger:

```bash
sose run \
  --config sose.toml \
  --trigger-id scheduler-2026-09-28T18:00 \
  --recover
```

## Inspect without advancing

```bash
sose inspect --config sose.toml
```

The command reads the durable job checkpoint, persisted domain configuration,
and SimulationPosition. It does not initialize or advance the job.

## Discover builtin domains

```bash
sose domains
```

## Discover persistence adapters

```bash
sose persistence
```

## Scheduler usage

A simple cron-style deployment can run:

```text
external scheduler
      |
      v
sose run --trigger-id <external-run-id>
      |
      v
one durable tick
      |
      v
process exits
```

The selected persistence adapter, not the process lifetime, owns the job
checkpoint.

That same CLI contract can later be used from Airflow, Databricks Jobs,
Kubernetes CronJobs, GitHub Actions, or another orchestrator.
