# OD-5a fenced recurring source-to-boundary — #490 initial integrated slice

Tracking: #490 under #477/#485. Requires merged #487, #488 and #491.
**Scientific freeze: NOT_ADMITTED.** Not native SimPy resource-arbitration
promotion or evidence for the five-arm confirmatory run.

## PRD and invariant

One scheduled invocation may produce **at most one committed causal phase**
of an existing organization's physical→business-effect chain.
The read-side reconciler gets first refusal. Its only inputs are **committed**
physical `DomainEvent`s; no invented receipt, ACK or observation is allowed.

When there is no pending committed physical boundary work, the existing
`SimulationJobState` runner may advance exactly one physical source step
through a disposable SimPy event callback. Source event, reservation ledger
and runner checkpoint commit in the same fenced UoW.

## Technical design

- `advance_od5a_integrated_occurrence` validates the current source/job
  checkpoint before allowing **any** boundary or physical mutation.
- On PostgreSQL it holds the existing scoped per-organization advisory **session**
  lock across plan→one step, reusing the exact `od5a:recurring:<job_id>`
  key used by standalone source and recurring checkpoint writes. No global
  clock, no global lock and no serialized unrelated organizations.
- The existing `reconcile_od5a_boundary_step` remains authoritative for
  **published → claimed → acknowledged → materialized → certified**;
  its five phases stay in separate durable transactions. No ACK/certificate
  is merged into the source transaction, preserving worker-death windows.
- The existing `run_od5a_recurring_tick` owns the source lease, stable
  externally persisted source trigger, epoch, expiry, recovery and
  `SimulationJobState`; it is invoked only when reconciliation yields `idle`.
  A caller must replay the same source trigger after ambiguous COMMIT.
- A terminal no-action source transition sets `done` without incrementing
  physical `source.step`, but it commits a separate runner occurrence and
  consumes its deterministic trigger. Its checkpoint must reflect precisely
  `run_count == source.step + 1` at terminal state, whereas every active
  physical source must maintain `run_count == source.step`. A completed
  ID always agrees with runner `run_count` and survives replay.
- `blocked` does not authorize new source progress.

## TDD and admission gates

This bounded increment tests stepwise conformance against #488 (not just
terminal metrics) on SQLite and real PostgreSQL: physical state and reservations,
full DomainEvent identity, typed messages, deliveries, ACK receipts, pending
Commands, separate observations and immutable effect certificates. A
two-organization SQLite test checks independent progress; a terminal restart
test rejects phantom source steps.

Before promoting #490 as fully certified, independently falsify real OS kills
**before and after each of the five independent commits**, DB restart and
lost-response replay, multi-process same-org contention, two independent
PostgreSQL organizations, wrong-domain routing, expired delivery claims,
stale source epochs, and three-way physics conformance. These are **remaining
gates**, not accomplishments implied by this first integrated slice.
Keep historical Manufacturing/O2C/MRO v1 replay gates, coverage >=95,
no unreviewed P1/P2, and OD-5a scientific freeze NOT_ADMITTED.
