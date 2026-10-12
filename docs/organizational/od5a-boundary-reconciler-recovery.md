# OD-5a committed-source boundary reconciler — one durable phase per call

Tracker: #483, under #477 and scientific program #461. Predecessors: merged #479 (typed physical event → fenced ACK + Command) and #482 (versioned downstream observation + independent certificate).

## PRD

A worker, invoked recurrently by its host, must recover the **boundary pipeline** for a specifically selected OD-5a organization after OS process death, using only the committed physical event DAG and authoritative OLTP state. Physical execution has already happened and is not re-run by this reconciler. A caller running it repeatedly must observe exactly one committed phase per successful call, never a synthesized SimPy process or an extra physical reservation.

## Deterministic read plan and commit boundary

`reconcile_od5a_boundary_step(store, job_id, owner_id, now, lease_duration)` scans only the requested organization's persisted physical `DomainEvent`s, ordered by per-organization semantic `tick` and immutable event ID. It rebuilds the canonical typed `BoundaryMessage` and validates the physical event against the authoritative scheduler state before choosing one of five transitions:

1. **published** — idempotently create the canonical boundary message + pending delivery from the committed source event.
2. **claimed** — claim only this exact event/message for this organization under a durable owner, positive epoch and expiry fence; recover an expired claim by incrementing its epoch. The delivery is not processed by a second live owner.
3. **acknowledged** — only the current lease owner may atomically ACK and stage the typed per-event `Command`. A stale worker receives **blocked** and cannot commit.
4. **materialized** — apply the validated staged Command into a separately committed version-1 terminal `od5a_physical_observation`; command and certification remain independent.
5. **certified** — verify the already materialized terminal observation, original event DAG and fenced transport receipt; atomically insert `BusinessEffectApplied` and retire the pending Command.

Prior fully certified events are audited and skipped, never re-executed. `blocked` returns an optional lease retry time; `idle` means no committed physical boundary work is pending but the source scheduler may still advance independently; `complete` requires the source job's durable `done` and all currently committed physical events certified. The caller explicitly supplies transport `now` (timezone-aware); no global organization logical clock is allocated.

## Test strategy and falsification

- All five admitted reference arms: compare number of the five phases to actual, finite physical event count; the `DomainEvent` DAG, physical reservation state and source scheduler Entity must remain untouched.
- Ten real OS-death scenarios around **each** of the five durable phases, testing both kill before invocation and kill after COMMIT but before response; re-open SQLite and compare full durable snapshots to uninterrupted control.
- Expired claim fencing: a second live owner remains blocked until expiry; then it reclaims with higher epoch while the old lease cannot ACK.
- Interleaved organizations: compare full shared-storage delivery/consumption/fenced epochs, physical clocks, messages, commands and certified observations against isolated advancement on the same source worlds.
- No read-side advancement of the uncommitted physical scheduler. Fail closed on forged source, ACK, Command or certificate; rely on the already reviewed #479/#482 validators.
- Review all exact-head CI checks and the three immutable Manufacturing/O2C/MRO v1 replay gates before merging.

## ADR / limitations

This is a **read-side, stepwise operational boundary reconciler**, **not** a production recurring SimPy-backed execution job. It proves only what its actual tests falsify: SQLite OS-boundary recovery, durable transports/effects, local claims and organizational isolation. Further #477 gates must explicitly test invocation inside the real recurring job, multiple OS workers with active same-job races, PostgreSQL backend in the combined pipeline, mid-UoW/process death, DB/network restarts, resource contention/durable waiting, and SimPy-vs-reducer-vs-recovered full physical event DAG equivalence. An existing generic CI PostgreSQL gate does not satisfy those OD-specific properties. Frozen historical scientific bundles remain unchanged and the prospective OD-5a scientific freeze remains `NOT_ADMITTED`; no confirmatory 17,920-run DOE is authorized.
