# ADR-0009 — Owner-qualified durable runtime only

- **Status:** Proposed; acceptance blocked by #504 / PR #506 exact-HEAD gates.
- **Date:** 2026-10-14
- **Scope:** SOSE core runtime, durable persistence contracts, organization composition and recurring jobs.
- **Related:** #494, #495, #496, #504, #506; ADR-0007 historical scientific freezes.

## Decision

SOSE has **one** authoritative execution model: every live runtime is explicitly owned.
`owner_id` is required, non-empty and immutable across a worker's recovery lifetime.
It is not an alias for `organization_id`, `job_id`, `correlation_id`, or the StateChart
entity type. An organization may own several independently scheduled jobs; the owner
contract must identify the exact temporal/command authority for each runtime.

The following are prohibited for live execution:

1. `owner_id=None`, unowned `ScheduledWork` and inferred owner from a process correlation.
2. Namespace-wide scalar `SimulationPosition`, committed tick and scenario state.
3. A second, legacy/unscoped backend and silent fallback reads or writes.
4. A namespace-wide lock/clock as a workaround for concurrent organizations.
5. Sending another owner's `ScheduledWork` into the local callback/StateChart.
6. Blind overwrites of owner positions by obsolete workers or by lost-update retry.

`Engine`, `RuntimeRebuilder`, `DurableScheduler`, job/domain runtime factories,
temporal/scenario storage and diagnostics must require a non-empty owner. The owner
must be carried across runtime creation, events/commands, scheduling, persistence,
rebuild, dispatch, checkpoint and forensic evidence.

**Identity vs ownership:** globally unique command/work IDs must be derived from
the issuing owner *and* their deterministic local inputs. Business entity identity
and typed causal edges are separate and must not be silently rewritten.
A cross-owner callback, cancel or collision must fail closed before any state
mutation. Sequence allocation and position advances are per-owner, with a
transactional compare-and-set/fencing epoch for independent same-owner workers.

Shared finite physical pools continue to use their own authoritative resource
ledger and transactional arbitration. That ledger is not a process clock.

## Break and migration

This is a deliberate breaking API/data-contract change, not a deprecation.
Existing unowned records are **not** adopted as any organization's history.
Admission of an unowned or mixed-mode active namespace fails explicitly.

For data that must continue to operate, a separate offline migration/rebuild
must require an operator-supplied ownership mapping, verify referential
integrity/causal parents/resource occupancy, write a new versioned namespace,
compare its immutable evidence hashes, and retain a rollback copy. It must
not start workers while rewriting or convert ambiguous ownership automatically.

Frozen Manufacturing/O2C/MRO v1 scientific artifacts and their hashes remain
immutable under ADR-0007. Read-only, version-pinned replay is an independent
scientific gate; no live legacy runtime code is kept solely to replay old data.

## Falsifiable acceptance

- RED contracts: omitted/empty owner rejected at every public entry point;
  previous scalar access impossible; foreign cancellation and callback refused.
- RED->GREEN: two same-type StateCharts in distinct owners can schedule the
  same semantic command without command/work ID collisions and with per-owner
  sequence, scenario state, clock, tick and checkpoints.
- PostgreSQL forced barrier at position read -> rebuild -> dispatch -> COMMIT,
  two real workers and independent connections; no silent shared clock.
- Crash/lease/fencing: stale epoch cannot commit even after its worker restarts.
  Continuous vs killed-and-recovered traces match *per owner*, including
  Commands, DomainEvents, receipts/ACKs, scheduled work, store effects and
  physical reservations; no duplicate or lost certified effect.
- SQLite and PostgreSQL restart; all supported adapter contracts; Python
  3.12/3.13/3.14, coverage threshold, historical replay and independent review
  succeed on the **same exact HEAD** before this ADR is marked Accepted.

## Status note

PR #506 currently implements only some owner-qualified paths; residual optional
owner parameters, scalar compatibility and missing job/domain propagation remain
blockers. This ADR describes the intended gate, not a claim of certification.
