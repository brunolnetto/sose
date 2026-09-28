# Durable recurring simulation jobs

SOSE supports two execution styles:

1. end-to-end execution for examples/tests;
2. recurring durable jobs that advance exactly one logical tick per trigger.

The recurring model is the intended orchestration boundary for production-style
jobs.

## Job lifecycle

A `SimulationJob` owns durable orchestration state:

- job id;
- domain name;
- serialized validated domain config;
- config revision;
- status;
- bootstrap completion;
- logical time;
- next logical tick;
- successful run count;
- domain bootstrap state;
- last trigger timestamp;
- last operational error.

The semantic runtime position remains authoritative. If a process crashes after
the tick transaction commits but before the job checkpoint is updated, the next
trigger resumes from `Persistence.simulation_position()`, not from stale job
metadata.

## One trigger = one tick

External orchestration calls:

```python
result = job.run_tick()
```

The runner:

1. initializes/seeds the domain once if needed;
2. loads the persisted config revision;
3. resolves the latest durable simulation position;
4. builds a fresh context/engine;
5. reconstructs a fresh ephemeral backend;
6. advances exactly one logical tick;
7. lets the domain reconcile tick-specific business behavior;
8. persists the job checkpoint.

This makes the host scheduler replaceable:

```text
cron / Airflow / Databricks Job / Kubernetes CronJob / scheduler
                         |
                         v
                  SimulationJob.run_tick()
                         |
                         v
                 durable persistence
```

The scheduler is a trigger. It is not semantic truth.

## Domain reconciliation

`DomainDefinition.reconcile_tick` is optional.

Core owns the fixed logical tick and durable mechanics. A domain hook owns
business-specific reconciliation at the boundary.

For example, MRO may:

- seed configured spare-parts availability;
- reconcile material availability;
- acquire technician/maintenance-bay capacity;
- consume parts;
- react to scenario-owned emergency pressure.

The generic job runner must not know any of those concepts.

## Configuration changes

A job stores its resolved domain config as JSON with a monotonically increasing
config revision.

```python
job.update_config({"technician_capacity": 3})
```

Mapping updates are patches over the previous resolved config, so unspecified
values remain unchanged.

A tick uses one config snapshot. New configuration takes effect on the next
trigger.

Business changes that are part of the simulated world should still be modeled
as domain state/events rather than configuration mutation.

## Pause/resume/failure

Jobs may be paused and resumed without changing simulation position.

A failed trigger stores status=`failed` and an operational error string.
A later trigger may retry from the latest committed semantic position.

## Persistence portability

Job state is part of the Persistence contract, so it is stored by:

- MemoryPersistence;
- snapshot SQLite;
- incremental SQLite;
- JSONL journal;
- DuckDB;
- future Postgres/warehouse adapters.

Every persistence conformance suite verifies durable job-state round trips and
rollback.

## End-to-end remains useful

End-to-end helpers remain valuable for:

- Reference documentation;
- tests;
- local demos;
- benchmark fixtures.

They are no longer the only execution model.

The product-facing path should converge on:

```text
parameterized domain
      +
persistent job
      +
one tick per external trigger
      +
chosen persistence target
```
