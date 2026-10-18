# OD-5a shared physical capacity wait — bounded operational contract (#477)

## PRD

When native SimPy has an ephemeral local permit but the *authoritative* shared
PostgreSQL physical pool denies its exact `start_at/end_at` interval, the
organization must not report a terminal domain error, commit a fake physical
reservation, emit a DomainEvent, or advance the SimulationJobState clock.

## TRD

- Only `ResourceCapacityError` raised during an explicitly bound
  `SharedPhysicalGrantAuthority` commit may be converted to `waiting`.
  Fail-closed `ResourceConflictError`, stale source/epoch, missing owner,
  invalid cause, or unmatched native permit still **raise**.
- The native physical/source transaction rolls back first. A *new*
  organization-scoped fenced UoW revalidates claim epoch, source snapshot and
  owned physical reservations and persists a single
  `od5a_shared_capacity_wait` Entity keyed by (organization, original trigger).
  It captures exact immutable scheduled action, logical due, pool binding
  fingerprint, native reservation ID, request ID and physical instance.
  The record retains `origin_epoch` and monotonic `last_claim_epoch`; each
  write checks the trusted DB timestamp against the original claimed lease.
- The job stays `running/advance`, with its original claimed lease and trigger;
  `OD5aRecurringStep(action="waiting")` leaves `logical_time`,
  `next_tick`, events and resource grants unchanged. Repeated same-owner
  attempts are idempotent. A worker takeover still obeys lease expiration,
  `recover=True`, fencing and the original deterministic trigger ID.
- On a subsequent *admissible* authoritative grant, the wait becomes
  `satisfied` in the **same SQL transaction** as the physical booking,
  source transition, causal DomainEvent, SimulationJobState checkpoint and
  released runner lease. A crash cannot commit only one side.
- Independent organizations sharing the pool may commit on separate physical
  instances without adopting another owner's logical clock.

## Falsification

The live PostgreSQL integration test first creates a real
`TemporalOutage` covering unit-1 at the source due. Two attempted ticks
persist one wait and zero source events/bookings. A peer on unit-2 advances.
PostgreSQL is then reopened and the outage is recovered **after** the
original due time. The original service interval remains historically
unavailable, so the same trigger MUST remain waiting: **no retroactive booking,
source event or completed trigger** is allowed. Full physical ledger audit
is required. The competing-worker test also verifies one winner and one
durably waiting loser under two actual PG connections/SimPy callbacks.

## Explicit stop rule

This is **not** a general physical scheduling/fairness solution. If another
owner occupies interval `[0,0.2)`, merely releasing that owner's booking at
`0.2` does *not* make a request for `[0,0.2)` admissible. A separate,
prospectively specified temporal rescheduling, priority/fairness and
preemption contract is required; do not rewind events or forge a free past
slot. Worker/process/network and server-commit uncertainty, five-arm shared
pool conformance, non-confirmatory evidence bundle and full scientific
admissibility remain open in #477/#461. OD-5a remains `NOT_ADMITTED`.
