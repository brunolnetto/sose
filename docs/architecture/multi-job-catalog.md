# Multi-job Engine Store catalogs

SOSE can host multiple durable simulation jobs in one physical authoritative
Engine Store while keeping each job's operational state isolated.

The deployment model is:

```text
physical Engine Store
├── job namespace A
│   ├── SimulationJobState
│   ├── Resources / Stores / Containers
│   ├── scheduler / scenario / sink state
│   └── fencing epoch
├── job namespace B
│   └── ...
└── job namespace N
    └── ...

job A ──> Domain Store A
job B ──> Domain Store B
job N ──> Domain Store N
```

The same domain may be instantiated more than once. Different domains may also
share the same Engine Store. Domain Stores remain independently configurable per
job.

## Configuration

A catalog uses one shared `[engine_store]` and one or more `[[jobs]]`
entries:

```toml
[engine_store]
adapter = "sqlite_incremental"

[engine_store.options]
path = "state/engine.sqlite3"

[[jobs]]
id = "mro-goiania"
engine_namespace = "mro_goiania"

[jobs.domain]
name = "mro"

[jobs.domain.parameters]
enabled = true

[jobs.domain_store]
adapter = "sqlite"

[jobs.domain_store.options]
path = "state/mro-goiania.sqlite3"

[[jobs]]
id = "orders-brasil"
engine_namespace = "orders_brasil"

[jobs.domain]
name = "order_to_cash"

[jobs.domain_store]
adapter = "postgres"

[jobs.domain_store.options]
dsn_env = "ORDERS_DOMAIN_DATABASE_URL"
namespace = "orders_world"
```

`engine_namespace` is optional. When omitted, SOSE derives a stable,
SQL-safe namespace from the job id. Explicit namespaces must be unique.

Catalog Engine Stores currently support `sqlite_incremental` and `postgres`.
Both provide real physical sharing:

- SQLite: independent record/meta table pairs live in the same SQLite file.
- PostgreSQL: independent record/meta table pairs live in the same database.

Each namespace has independent revision and fencing metadata, so writer ownership
for one job does not fence another job.

## Python

```python
from sose.api import build_job_catalog_from_file

with build_job_catalog_from_file("sose-catalog.toml") as catalog:
    mro = catalog.get("mro-goiania")
    orders = catalog.get("orders-brasil")

    mro.run_tick(trigger_id="mro:2026-10-01T12:00")
    orders.run_tick(trigger_id="orders:2026-10-01T12:00")
```

A job only sees the durable operational state in its Engine namespace. Closing
one catalog closes every Engine Store connection and Domain Store owned by its
jobs.

The existing single-job `sose.toml` contract remains supported unchanged.
