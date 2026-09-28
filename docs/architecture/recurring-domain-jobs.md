# Recurring domain jobs

SOSE builtin domains support two independent contracts:

```text
DomainConfig
    |
    +-- validated parameters and defaults
    |
DomainDefinition
    |
    +-- seed()
    +-- build_runtime()
    +-- reconcile_tick()
```

The first controls **what the domain means for this job**. The second makes that
domain executable one durable logical interval at a time.

## One external trigger = one durable tick

`SimulationJob.run_tick()` does not run a Reference end-to-end.

For each trigger it:

1. claims the trigger durably;
2. rebuilds runtime/backend from semantic persistence;
3. advances exactly one logical tick;
4. commits `SimulationPosition`;
5. invokes the domain's `reconcile_tick()`;
6. commits the durable job checkpoint;
7. returns control to the external scheduler.

Therefore cron, Databricks Jobs, Airflow, Kubernetes CronJob, an Azure scheduler,
or another orchestrator can call the same job repeatedly without SOSE needing a
permanent process.

## Reconcile contract

`reconcile_tick(persistence, engine, backend, config, bootstrap_state)` owns
domain meaning after the generic time boundary has committed.

It must be:

- idempotent across process restart;
- driven by durable entity/resource/store/container state;
- safe when called after a crash at the same reconciliation phase;
- explicit about configured happy/sad-path choices;
- free of hidden end-to-end loops.

A reconciler may perform several atomic operations that belong to one business
boundary, but future time must remain represented by ScheduledWork rather than
by calling `backend.run_until(...)` into the future.

## Parameterization

Every builtin Reference has its own `DomainConfig`.

Common parameters are:

- `start_at`;
- `tick_step`;
- `random_seed`.

Domain-specific parameters own domain meaning. Examples include:

- capacities and appointment windows;
- quantities and inventory policy;
- payment/settlement/close delays;
- underwriting or assessment outcome;
- departure/arrival boundaries;
- service/product/route identifiers;
- whether recurring automation should seed required operational supply.

Unknown parameters are rejected.

A durable job stores the resolved config JSON and a monotonically increasing
`config_revision`. Editing `sose.toml` has no implicit effect on an existing
job until `sose apply` explicitly updates the durable configuration.

## Boundary ordering

The important ordering is:

```text
external trigger
      |
      v
Engine.advance_tick()
      |
      +-- execute due ScheduledWork
      +-- persist SimulationPosition
      |
      v
DomainDefinition.reconcile_tick()
      |
      +-- reconcile resource ownership
      +-- materialize domain evidence
      +-- schedule future semantic boundaries
      |
      v
SimulationJobState checkpoint
```

Domains whose first semantic boundary must exist before tick 1 schedule that
boundary during `seed()`. Transit trip start/end boundaries are an example.

## Reference coverage

The builtin catalog now requires `reconcile_tick` for every promoted Reference
plus the tutorial domain. The executable recurring-job suite covers both the
presence of the hook and representative multi-trigger progression.

End-to-end helpers remain useful as compact specifications and regression
fixtures. They are no longer the only way to execute a Reference operationally.
