# PC6 domain-local scheduled-work recovery boundary (#468)

Status: bounded TDD corrective increment; does NOT settle shared-clock/store contention.

## Evidence and defect

Real two-worker PG stress (run 38411282052, 9 PASS / 3 FAIL) showed a local
Logistics engine attempting to execute an unexpected `shipment` scheduled
callback in a context whose `DomainRegistry` did not own `shipment`. That
raises `KeyError('shipment')` after the complete pending schedule from a
shared PostgreSQL namespace was reconstructed into a foreign ephemeral backend.

`RuntimeRebuilder` previously rebuilt **every** persisted `ScheduledWork`
into every local runtime backend, without checking its domain registry.
A domain-specific engine can only bind its own chart types; foreign callbacks
must remain authoritative pending work for their owning runtime.

## Invariant and TDD

- A domain engine rebuilds callbacks only where `registry.supports(command.entity_type)`.
  The generic `RuntimeRebuilder` retains its original **unfiltered** behavior
  when the optional `scheduled_work_filter` is not supplied.
- Foreign `ScheduledWork` and `Command` are **preserved**, not deleted,
  rescheduled, acknowledged or silently converted to successful execution.
- Recovery boundary validation applies to the **selected domain's** scheduled
  work. A foreign pre-boundary schedule is not that engine's command to repair.
- Existing same-domain scheduled callbacks still dispatch and commit atomically
  and duplicate retry remains idempotent.
- Selection by *type* cannot distinguish independent organizations running
  the **same** StateChart type. Full job/owner scope, safe clock/position
  independence and shared physical resource arbitration are still required
  by #468, #477 and #496.

RED tests: `tests/unit/sose/core/test_runtime_rebuilder.py` includes a mixed
work/foreign schedule and a foreign item earlier than selected recovery boundary;
`tests/unit/sose/core/test_durable_engine.py` exercises an actual local MRO
runtime holding a foreign shipment command and ensures its callback never
enters MRO. All historical one-domain `RuntimeRebuilder` contracts still run.

GREEN code is isolated to `Engine.rebuild_backend`, optional
`RuntimeRebuilder.scheduled_work_filter`, and the `DomainRegistry.supports`
query. No change to event ownership, authoritative persistence, fencing,
`SimulationPosition` layout or scientific frozen protocol.

## Remaining scope / stop rule

The same stress run also showed concurrent workers rebuilding from a shared
`SimulationPosition`, which could be advanced after one worker sampled its
position. **That is a separate unresolved clock-ownership/transaction-snapshot
fault**. The live `PostgresPersistence` still permits last-writer-wins on
same-record conflicts, and independent jobs of the same domain may still
observe each other's scheduled work without a job-scoped filter. Follow-up
requires deterministic RED tests and validated job/flow identity, not an
organization-global mutex or global tick.

Do not mark #468 or #477 complete based on this PR. OD-5a remains
`NOT_ADMITTED`; immutable v1 frozen artifacts are untouched.
