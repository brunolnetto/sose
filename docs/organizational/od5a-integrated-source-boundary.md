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

- `advance_od5a_integrated_occurrence` validates source/job
  checkpoint and **both** lifecycle forms of source lease **before**
  downstream mutations: a released fence for the last completed occurrence,
  or the claimed fence for a still-active occurrence. Active history must
  preserve deterministic next-trigger identity, epoch, owner, expiry and
  job phase, but an expired historical claim does not prevent delivery of
  previously committed physical events. Actual physical source advancement
  independently checks its live claim with trusted database time.
- PostgreSQL boundary phases use `FencedEnginePersistence` with a fresh
  scoped writer epoch for `od5a:boundary:<organization>`, obtained under the
  same organization guard. These take shared namespace causal boundary
  locks plus per-organization locks; independent organizations do **not**
  acquire the legacy exclusive namespace-wide causal lock. Legacy unscoped
  boundary operations remain mutually exclusive with scoped operations.
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
- The reducer marks `done` during the **last real physical action**,
  within the same transaction that increments `source.step`, persists the
  event, and advances the recurring `run_count`. Therefore the invariant is
  `run_count == source.step` in **both** active and completed organizations.
  An authoritative terminal source can **never** retain an active
  runner trigger; the shared checkpoint reader rejects any such forged
  lifecycle before read-side reconciliation or replay.
  That action returns `advanced`; later queries using the last committed
  deterministic trigger return `replayed`, while a next-trigger completion
  probe returns `complete` **without committing or consuming** a new trigger.
  A reconstructed worker verifies this history before processing downstream
  facts, without inventing a terminal SimPy callback or physical event.
  Once all boundary events are certified, retrying the **last committed**
  source trigger returns `source_replayed`; only the **next deterministic**
  trigger returns `complete`. Arbitrary terminal trigger IDs are rejected.
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
