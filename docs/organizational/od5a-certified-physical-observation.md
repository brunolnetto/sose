# OD-5a per-event physical observation and independent effect certificate

Tracking: #480 (parent #477). Predecessor: merged #479. **Non-confirmatory** production-boundary increment; OD-5a execution freeze remains `NOT_ADMITTED`.

## PRD — five distinct durable facts

1. `od5a.execution.v1` or `od5a.coordination.v1` is an actually committed physical `DomainEvent`, validated against the authoritative per-organization scheduler stage-completion witness.
2. `od5a.physical_completion.v1` `BoundaryMessage` refers to that immutable event through `causation_kind=event`.
3. Its consumed `BoundaryDelivery` and `BoundaryConsumption` prove successful fenced transport **ACK**, not completion of the destination domain effect.
4. The ACK transaction creates a deterministic `od5a.stage_physical_completion.v1` `Command` keyed to the **physical event**, not the organization-wide scheduler. A later UoW creates a distinct version **1** terminal `od5a_physical_observation` `Entity`, with immutable event/organization/model/config identity, per-instance resource class, exact microsecond interval, work item/station, and parent event list. Merely having a Command is not proof that this step committed.
5. Only after that exact persisted observation exists can a separate UoW save `BusinessEffectApplied` and retire the staged Command **atomically**. The certificate references the typed source BoundaryMessage, immutable physical event observation, terminal state and version. Replaying after COMMIT returns the original certificate; a forged certificate or observation fails closed.

## TRD / falsifiers

- `apply_od5a_physical_observation(store, effect_id, applied_at)`: validate the registered contract, claimed+consumed receipt, exact upstream event and physical scheduler witness, downstream Command and source payload, then materialize one versioned observation. Idempotent under replay; rejects conflicting pre-existing entities.
- `certify_od5a_physical_observation(store, effect_id, completed_at)`: revalidate all facts inside the transaction and certify only an **already committed** terminal observation; paired certificate insert and Command deletion cannot split under rollback.
- Apply and certify use the same boundary-scoped transaction mechanism as transport and existing PC6 certificates. No shared organization-wide logical clock is introduced.
- TDD integration tests exercise five treatment arms, both physical kinds, corrupted observations/commands, wrong version, real OS worker death after materialization or certification COMMIT, rollback before COMMIT, and two independent organizations on one SQLite database.
- Existing `BusinessEffectService.complete` and its PC6 `_CERTIFIED_TERMINALS` remain unchanged: OD-5a cannot obtain a PC6 terminal certificate by calling generic completion.
- Historical Manufacturing/O2C/MRO v1 immutable scientific replays remain mandatory checks. PostgreSQL and distributed worker kill-point certification remain prerequisites for #477; passing the existing CI PostgreSQL infrastructure gate alone does not prove the new OD-specific operations in PostgreSQL.

## ADR — scope and stop rule

A certificate is **downstream materialization evidence**, not a second fabricated physical event or a resurrected SimPy reservation. The five-arm, 17,920-run confirmatory DOE, prospective scientific hash seal, full production runner and PostgreSQL failure matrix are not authorized by this PR. This slice is not the end of #477 or #480 until all stated acceptance gates are demonstrated.
