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
  --sqlite state/trading.sqlite3 \
  --owner-id pc6-recovery-1 \
  --start-at 2026-10-09T12:00:00+00:00 \
  --interval-seconds 60 \
  --max-slots 16 \
  --max-actions 16
```

Or use the long-running worker with `--serve --poll-seconds 5`. The
`--now` override exists for controlled one-shot replay/testing only.

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
