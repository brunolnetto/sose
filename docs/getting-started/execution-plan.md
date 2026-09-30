# Declarative execution plan

`sose plan` resolves the complete job configuration without opening any
persistence or analytical connection.

```bash
sose plan --config sose.toml
```

It combines four product-facing concerns:

```text
DomainConfig
    +
StoragePlan
    +
Runtime
    +
Recurring Job Policy
```

Example:

```json
{
  "domain": {
    "name": "mro",
    "config_model": "MROConfig",
    "parameters": {
      "quantity": 2.0,
      "auto_seed_spare_parts": false
    },
    "runtime_mutable_fields": [
      "auto_seed_spare_parts",
      "random_seed",
      "tick_step"
    ]
  },
  "storage": {
    "authoritative": {
      "adapter": "postgres",
      "role": "authoritative",
      "durable_recurring_ready": true
    },
    "analytical": [
      {
        "name": "lakehouse",
        "adapter": "databricks",
        "role": "analytical"
      }
    ],
    "issues": []
  },
  "runtime": {
    "backend": "simpy"
  },
  "job": {
    "id": "mro-daily",
    "ticks_per_trigger": 4,
    "max_ticks_per_trigger": 20,
    "execution_model": "durable_recurring_trigger",
    "external_scheduler_owned": true
  }
}
```

## Why plan is separate from validate

`sose validate` answers whether the file is valid.

`sose plan` answers what SOSE would execute.

The plan resolves domain defaults, parameter overrides, capability requirements,
storage roles, runtime backend, and recurring tick policy into one view.

## No side effects

Planning must not:

- create a SQLite/DuckDB file;
- open PostgreSQL;
- require Databricks/Snowflake credentials;
- initialize or seed a durable job;
- publish analytical events;
- advance logical time.

This makes it safe for:

- CI configuration checks;
- deployment previews;
- generated infrastructure;
- UIs;
- code review;
- scheduler validation.

## Suggested workflow

```bash
sose init --domain mro

sose config set auto_seed_spare_parts false
sose storage set-authoritative postgres \
  --require process_durable \
  --require transactional_commits \
  --require concurrent_writers \
  --require remote \
  --option dsn_env=SOSE_DATABASE_URL \
  --option namespace=mro_job

sose storage add-sink lakehouse databricks \
  --option server_hostname_env=DATABRICKS_SERVER_HOSTNAME \
  --option http_path_env=DATABRICKS_HTTP_PATH \
  --option access_token_env=DATABRICKS_TOKEN

sose job set-policy \
  --ticks-per-trigger 4 \
  --max-ticks-per-trigger 20

sose plan
sose validate
sose doctor
```

After deployment, the external scheduler invokes:

```bash
sose trigger \
  --scheduled-for 2026-09-30T09:00:00-03:00
```

The durable job checkpoint determines where execution resumes.
