# PC6 / #504 — Owner-scoped recovery and scheduled-work binding (TRD proposal)

Status: **proposed, not implemented**. This document accompanies the first
deterministic RED counterexample in draft PR #505. It is not a certificate of
distributed recovery or of OD-5a scientific admissibility.

## Observed defect

In the legacy Engine, `Persistence.simulation_position()` has a single value
for a namespace; `Engine.advance_tick()` updates that value and
`RuntimeRebuilder.rebuild()` validates a backend against it. A second worker
can commit a later position between another worker's position read and
reconstruction. The current `scheduled_work_filter` checks that the registry
supports the command's entity *type*, not which organization/job owns it.

The initial same-type PostgreSQL RED test forces A(read) →
B(advance+commit) → A(rebuild) without timing luck, **within one legacy
unscoped namespace**. It demonstrates that the legacy clock can change between
the position sample and rebuild. It does NOT yet demonstrate two independent
organizations: `instance_key` differentiates entity IDs, and fixed
`flow_correlation_id()` is not an organization binding. The second gate
requires persisting explicit org/job ownership and constructing each runtime
through its scoped view before owner-isolation assertions are meaningful.

## Ownership contract

Use distinct, immutable identity axes:

- `organization_id`: durable logical organizational execution boundary.
- `job_id`: independent recurring execution within that organization.
- `writer_epoch` and cursor revision: fencing/optimistic concurrency metadata,
  not business identity.
- `entity_type`: registry/routing metadata; **never** an ownership key.
- `correlation_id`: causal trace across domains; **never** an ownership key.
- `resource_scope` and physical instance: arbitrated independently of clocks.
- `work_id` / `command_id`: immutable work and causal identities.

The initial operational cursor key is
`(namespace, organization_id, job_id)`. Persist a versioned owner binding for
each `ScheduledWork` reference and validate the bound Command's identity.
An organizational composition may operate multiple jobs with different
clock boundaries. No inferred owner from a shared StateChart class.

## Transaction and recovery semantics

1. Read the owning cursor plus pending work from one consistent
   owner-scoped snapshot; validate expected revision before dispatch/commit.
   Do not sample a global position and independently read a different work
   snapshot after a competing commit.
2. Atomically compare expected revision/epoch and advance **only** that
   owner's cursor while committing state, effects and work lifecycle changes.
   A stale writer must receive a definite conflict, not last-writer-wins.
3. Load only pending work bound to the rebuilding owner, even when another
   organization registers precisely the same StateChart type.
4. Validate callback binding before dispatch, ACK and terminal effect; a
   stale callback may not consume foreign ScheduledWork or advance its clock.
5. Preserve per-owner scenario state and committed tick when they affect
   behavior. Shared physical pool reservations remain in one transactional
   capacity ledger, not a global simulation clock.
6. Propagate uncertain COMMIT and worker death without inventing success.
   Reopen the authoritative store and resolve by immutable operation IDs,
   revision and fencing epoch.

Implementation candidates include owner-indexed versioned records or
owner-bound record keys. Their actual schema and migration must be selected
after profiling the SQLite reference, PostgreSQL record/delta adapter,
serialization and legacy job APIs. **Do not** introduce a global engine mutex
or an organization-wide shared tick to make the test green.

## Compatibility

Historical unscoped namespaces keep the existing API and deterministic v1
replays. A scoped namespace requires explicit owner bindings on every cursor
and ScheduledWork write/read. Legacy/unscoped plus scoped workloads in one
namespace must be rejected clearly unless an audited explicit migration
establishes a unique owner for each existing work item. Do not silently map
`correlation_id` or `entity_type` to ownership.

## TDD and promotion gates

- #505 first RED: forced two-connection, same-type PG interleaving, no
  timeout/retry disguising the global-clock hazard. Before GREEN, replace the
  *unscoped* synthetic fixture with explicit durable owner/job bindings.
  Owner A must then rebuild at its own clock and load only A callbacks.
- Follow-up RED: same-owner stale epoch/revision conflicts; different owners
  advance without serialization; mixed namespace rejects safely.
- Forced worker deaths at publication, claim, ACK, Command, physical
  reservation and effect; verify continuous vs recovered full owner snapshots,
  including Commands, DomainEvents, ScheduledWork, resources, store selections,
  positions, checkpoints and causal graph.
- SQLite reference and PostgreSQL must conform; Python 3.12–3.14, >=95%
  coverage, chaos/CI, independent review and historical Manufacturing/O2C/MRO
  v1 replay must pass at the exact production HEAD.
- Do not mark #504 complete or merge draft #505 on a passing random run.
  OD-5a's existing preregistered scientific gates are independent of this
  organizational-runtime certification.


## First implemented routing slice (draft #505, not a release gate)

`ScheduledWorkOwnerBinding(work_id, organization_id, job_id)` is an immutable
version-independent identity of an individual pending callback. The incremental
record collection `scheduled_work_owners` is supported by the Memory, SQLite,
and PostgreSQL record adapters. The optional `RuntimeRebuilder(owner_key=...)`
selects only explicitly bound callbacks even when their StateCharts have the
same `entity_type`. Activating scoped reconstruction with any unbound legacy
ScheduledWork fails closed; legacy unscoped engines keep their original APIs.

PostgreSQL treats ownership insertion as immutable: competing writers on the
**same** `work_id` cannot overwrite the first committed binding; different
work IDs can commit concurrently. This is implemented as record-key uniqueness
with a readback on conflict, not a namespace mutex. The regressions include
independent workers on distinct work and adversarial conflicting workers on
one work, plus SQLite reopen/delete and in-memory identity immutability.

This slice **does not yet** give a job a separate authoritative
`SimulationPosition`, a versioned CAS cursor, scoped tick or scenario state,
fenced owner-specific dispatch/ACK, or cross-domain physical/recovery
equivalence. It must remain in draft until those gates and the original forced
clock RED are GREEN. A typed callback owner is necessary, not sufficient, to
make `Engine.advance_tick()` an organizational operation.
