# Configure storage from the CLI

SOSE keeps one declarative file as the user-facing composition point.

You can inspect the current storage roles without opening any connection:

```bash
sose storage --config sose.toml
```

## Change the authoritative store

SQLite incremental:

```bash
sose storage set-authoritative sqlite_incremental \
  --config sose.toml \
  --require process_durable \
  --require transactional_commits \
  --option path=state/sose.sqlite3
```

PostgreSQL:

```bash
sose storage set-authoritative postgres \
  --config sose.toml \
  --require process_durable \
  --require transactional_commits \
  --require concurrent_writers \
  --require remote \
  --option dsn_env=SOSE_DATABASE_URL \
  --option namespace=mro_daily
```

The command validates the adapter and required capabilities before editing the
file. It does not open the database.

Changing `sose.toml` does not silently mutate a durable job. Domain
configuration changes still cross the explicit `sose apply` boundary, while
storage changes affect the next process/job construction because they select
where the job itself is persisted.

When changing authoritative persistence for an already initialized job, migrate
or intentionally bootstrap the durable state into the new target first. The CLI
does not copy state between databases implicitly.

## Add an analytical sink

JSONL:

```bash
sose storage add-sink audit jsonl \
  --config sose.toml \
  --option path=analytics/events.jsonl
```

Databricks:

```bash
sose storage add-sink lakehouse databricks \
  --config sose.toml \
  --option server_hostname_env=DATABRICKS_SERVER_HOSTNAME \
  --option http_path_env=DATABRICKS_HTTP_PATH \
  --option access_token_env=DATABRICKS_TOKEN \
  --option events_table=main.operations.sose_events \
  --option batches_table=main.operations.sose_batches
```

Snowflake:

```bash
sose storage add-sink warehouse snowflake \
  --config sose.toml \
  --option connection_name=sose \
  --option events_table=SOSE_EVENTS \
  --option batches_table=SOSE_BATCHES
```

Multiple sinks may be configured at once, but each sink name must be unique.

## Remove a sink

```bash
sose storage remove-sink lakehouse --config sose.toml
```

Removal changes only the declarative configuration. Existing analytical data is
not deleted.

## Failure behavior

Storage editing is validate-before-write.

These operations leave the TOML unchanged when:

- the persistence adapter does not exist;
- a required capability is unsupported;
- the sink adapter does not exist;
- a sink name already exists;
- an option does not use `KEY=VALUE` syntax.

This keeps configuration editing separate from runtime side effects.

## Recommended recurring-job flow

```bash
sose init --domain mro --output sose.toml

sose config set auto_seed_spare_parts false --config sose.toml

sose storage set-authoritative postgres \
  --config sose.toml \
  --require process_durable \
  --require transactional_commits \
  --require concurrent_writers \
  --require remote \
  --option dsn_env=SOSE_DATABASE_URL \
  --option namespace=mro_job

sose storage add-sink lakehouse databricks \
  --config sose.toml \
  --option server_hostname_env=DATABRICKS_SERVER_HOSTNAME \
  --option http_path_env=DATABRICKS_HTTP_PATH \
  --option access_token_env=DATABRICKS_TOKEN

sose validate --config sose.toml
sose doctor --config sose.toml

sose trigger \
  --config sose.toml \
  --scheduled-for 2026-09-30T09:00:00-03:00
```

The same command can be invoked by cron, Airflow, Databricks Jobs, Kubernetes,
or another scheduler. Durable job state determines where execution resumes.
