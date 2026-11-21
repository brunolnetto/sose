# PC6 durable recovery scheduling: PRD / TRD / ADR

**Status:** incremental implementation for issue #406. This is not a
certification of end-to-end Logistics or PostgreSQL multi-worker recovery.

## PRD: operational behavior

An operator must be able to resume a persisted PC6 recovery job without
replaying a Python stage sequence or guessing which invocation died. An
external scheduler (cron, Kubernetes CronJob, Azure Container Apps Job) may
invoke the command periodically. A continuously running polling worker is
also supported. The job runs bounded work, preserving existing trigger IDs.

Acceptance criteria:

- Each slot has a stable identity based on `job_id` and scheduled logical time.
- Restarts replay the earliest unfinished slot before advancing logical time.
- Duplicate invocation of an already checkpointed slot has no business effect.
- Recovery work is bounded by `max_actions`; catch-up is bounded by `max_slots`.
- Database writer leases fence prior owners on re-acquisition.
- Historical v1 scientific bundles and their hashes remain unchanged.

## TRD: checkpoint-derived deterministic schedule

`RecoverySchedule` persists **no process-local clock cursor**. It derives the
next due slot from the authoritative `SimulationJobState` in this order:

1. If an active trigger exists, retry `last_triggered_at`.
2. Otherwise if a completed trigger exists, choose its logical time + interval.
3. Otherwise if `last_triggered_at` exists, retry that first occurrence.
4. Otherwise start from the explicitly configured timezone-aware anchor.

The scheduler calls `TradingCustomerRecoveryRunner.run_scheduled_trigger`,
which calculates `scheduled_trigger_id`, fences its writer and commits a
`CompletedJobTrigger`. Invocations use the scheduled instant, **not** the
wall time of the worker, as the logical clock. This prevents downtime or
variable worker latency from changing causal occurrence identities.

Run once under an external scheduler (use the same anchor, interval and DB
on every invocation):

```sh
sose composition-recovery \
  --postgres-dsn postgresql://user@host/db --namespace trading \
  --resource-policies policies.json \
  --owner-id pc6-recovery-1 \
  --correlation-id <durable-flow-correlation-id> \
  --organization-id <authoritative-organization-id> \
  --start-at 2026-10-09T12:00:00+00:00 \
  --interval-seconds 60 \
  --max-slots 16 \
  --max-actions 16
```

`policies.json` maps each resource-backed intent to a pool contract, for example
`{"composition.deliver_shipment": {"resource_type": "pickup_courier", "pool_id": "couriers",
"scope": "shared", "capacity": 2}}`.

> **Removed in 0.11.0 (ADR-0011, #634):** the policy-less recovery mode and the
> `--sqlite` option, deprecated since 0.9.0. Composition recovery requires PostgreSQL
> (`--postgres-dsn`) and a resource-policy file (`--resource-policies`), because the
> physical resource ledger (`IntentResourceCoordinator`) is PostgreSQL-backed.
> `TradingCustomerRecoveryRunner` raises `TypeError` without `resource_policies`.

The operator must supply the real durable correlation and its authoritative organizational owner. `--owner-id` identifies the worker lease only; it must never be reused to infer Engine ownership. This command handles one correlation per invocation.

Or use the long-running worker with `--serve --poll-seconds 5`. The
`--now` override exists for controlled one-shot replay/testing only.

### The job-scoped lease, and re-binding a 0.13 global job (#637, #635)

Every run uses the job's own job-scoped writer lease, which never fences other
jobs, and stores the job identity `{correlation_id, organization_id}`. One worker
handles exactly one `--correlation-id`. `--scoped-writer` is still accepted, but it
no longer changes anything.

Up to 0.13, the command could run under the namespace-wide (global) writer lease,
which stored the identity `{}`. That mode was deprecated in 0.13.0 and removed in
0.14.0. A job created that way is refused ("created under the namespace-wide
writer lease ... re-bind it once") until it is re-bound. To re-bind it, stop or let
finish any 0.13 global worker of the job and run once:

```sh
sose composition-recovery --rebind-scoped \
  --postgres-dsn postgresql://user@host/db --namespace trading \
  --owner-id pc6-rebind-operator \
  --correlation-id <durable-flow-correlation-id> \
  --organization-id <authoritative-organization-id>
```

The operation (also `sose.composition.recovery_rebind.rebind_recovery_job_to_scope`)
claims the global writer lease. That lease is namespace-wide, so the claim fences
**every** global-lease writer in the namespace, not only this job's worker: a
still-running 0.13 recovery worker, and Engine or OD-5a runners holding the global
lease. Their in-flight transaction fails with `StaleWriterError`; a long-lived
holder of one global lease must re-claim it.

In one transaction fenced by that lease, the operation:
- checks the job;
- reserves the correlation for the job's scope;
- rewrites the identity to the scoped form.

The `job_id`, `run_count` and completed-trigger history stay the same, so the scoped
worker continues with the next slot and never repeats a completed one. The global
lease is then left unused, because scoped claims and scoped transactions never
consult it. The operation prints a JSON report. Running it again reports
`"status": "already_scoped"` and changes nothing.

It refuses:
- a missing job;
- a job of another domain;
- a job bound to another scoped identity;
- an occurrence still in flight;
- a correlation already reserved by another job;
- a job whose resource-intent links (admitted by its own triggers) name another
  correlation or organization.

That history check only sees recorded resource-intent links (PostgreSQL policy
mode). A global job never bound a correlation in its identity, so where no links
exist the operator's `--correlation-id` is trusted. The re-binding stays available
after 0.14.0, so a database written by 0.13 can be upgraded first and re-bound
afterwards.

## ADR: time, causality and the recovery boundary

- **Chosen:** stable scheduled slots, persisted job checkpoint, writer fencing,
  bounded retries. No implicit wall-clock trigger identities.
- **Rejected:** process-local timers/counters as durable scheduling authority.
- **Rejected:** treating an ACK as proof that a staged `Command` has executed.
- **Known limitation:** this worker currently resumes WF/WM only; it does not
  independently apply Logistics/O2C/Payments/R2R effects.
- **Known limitation:** two independent PostgreSQL workers and partition/crash
  interleavings are not yet certified.
- **Known limitation:** source event/command causation may still be untyped
  in frozen v1 histories; new typed causal references are opt-in.

Therefore issue #406 remains open until all end-to-end causal/concurrent
recovery acceptance criteria are satisfied.
