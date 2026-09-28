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
result = job.run_tick(trigger_id="airflow-run-2026-09-28T20:00Z")
```

An explicit `trigger_id` is persisted as orchestration ownership. Repeating a
successfully completed trigger returns the prior tick result without advancing
simulation time again. A different trigger is rejected while the job is already
running.

Callers that do not provide a trigger id still receive a deterministic
per-job/per-tick generated identity for the current process invocation.

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


## Trigger ownership

Recurring schedulers can retry or overlap executions. SOSE therefore persists:

- `active_trigger_id` while one trigger owns execution;
- `last_completed_trigger_id` after a successful tick.

The claim happens transactionally before backend reconstruction.

This gives two guarantees:

1. the same completed external trigger is idempotent;
2. a second trigger cannot execute the same job concurrently while ownership is
   active.

A process crash before completion can leave a running claim. Automatic lease
expiry is intentionally not part of this contract yet. Recovery of an
in-progress tick requires a separate phase/recovery contract so SOSE does not
guess whether a worker is still alive.

That recovery work is tracked separately because `advance_tick()` may commit
semantic position before domain `reconcile_tick` completes. The next job
hardening step must make the advance/reconcile/checkpoint phases resumable rather
than merely clearing a stale claim.


## Resumable tick phases

A recurring tick has two durable orchestration phases:

```text
advance
  |
  | Engine.advance_tick() commits SimulationPosition
  v
reconcile
  |
  | DomainDefinition.reconcile_tick()
  v
idle / completed trigger
```

`SimulationJobState.phase` persists one of:

- `idle` — no trigger owns unfinished work;
- `advance` — the trigger owns the next semantic tick;
- `reconcile` — semantic position advanced and domain reconciliation must finish.

The phase exists because semantic tick advancement and domain reconciliation may
cross multiple transactions.

### Crash after advance commit

If the process dies after `Engine.advance_tick()` commits but before the job
checkpoint changes to `reconcile`, the persisted job may still say
`phase="advance"`.

On explicit recovery, SOSE compares the job checkpoint with authoritative
`Persistence.simulation_position()`.

If durable logical tick is already ahead of the job's `next_tick`, the runner
infers that advance committed and resumes directly at reconciliation. It does
not execute another semantic tick.

### Crash during reconciliation

The job remains owned by the same trigger with `phase="reconcile"` and
`status="failed"`.

Retry requires the same trigger identity and explicit recovery:

```python
job.run_tick(
    trigger_id="airflow-run-42",
    recover=True,
)
```

A different trigger is rejected until the unresolved trigger completes.

Domain `reconcile_tick` implementations must therefore be restart-safe and
idempotent at the same durable boundary. This is the same design rule already
used by Reference reconciliation helpers.

### Why recovery is explicit

SOSE does not automatically expire a running claim based on wall-clock time.
The library cannot know whether an external worker is dead, slow, paused, or
partitioned.

The orchestrator decides when an attempt is no longer live, then retries the
same trigger with `recover=True`.

This keeps scheduler liveness policy outside semantic truth while preserving a
durable, deterministic recovery contract.
