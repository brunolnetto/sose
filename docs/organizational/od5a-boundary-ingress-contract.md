# OD-5a: durable physical event → fenced boundary ACK → Command ingress (non-confirmatory)

Tracker: #477. Parent design: #461. Relies on merged #473/#476 physical pilot, #474 authoritative reference reducer and #478 real multiprocess falsifier.

## PRD

A real, committed OD-5a physical execution or coordination interval should be able to reach the **existing** SOSE durable boundary delivery protocol without minting synthetic physical successes, borrowing another organization's causal identity, losing the admission/model hash, or treating an ACK as a certified business transition.

## TRD — first bounded implementation

- An immutable persisted `DomainEvent` is the only permitted cause. Reject an arrival, missing event, foreign organization's event, wrong `model_spec_hash`, config epoch, physical instance, or timestamp. The event must be an `od5a.execution.v1` or `od5a.coordination.v1` whose end time equals its authoritative `occurred_at`.
- Publication is an idempotent `BoundaryMessage` with `causation_kind=event`, model and pilot identity, resource instance, microsecond times, and event ID; the existing `BoundaryService` validates typed event ancestry inside its transaction. Publication may be retried after a worker crash without duplicating a delivery.
- Consumers claim only the registered `od5a.physical_completion.v1` destination and requested organizational correlation ID. The existing delivery's owner, lease expiry and epoch fence stale claims.
- Consumption validates the original persisted physical event **again in the ACK transaction**. One successful ACK atomically creates a deterministic staged `Command` targeting `entity_type=od5a_physical_observation` and `entity_id=<immutable DomainEvent.event_id>`, not the organization-wide scheduler. This preserves a one-effect-per-physical-event certification target for #480. Failures roll back receipt and intent; repeat delivery is idempotent. No observation or effect certificate is created by this PR.
- Tests reopen SQLite between publication, claim and consumption for each of the five treatment arms; inject lost claim/lease takeover, forged boundary payload, missing/non-physical/cross-org causes, and independent organizations.

## ADR — explicit non-goals

**A boundary ACK is not a certified business effect.** `od5a.stage_physical_completion.v1` is only a durable intent, and cannot be considered complete when a `Command` merely exists. `BusinessEffectService.complete` is intentionally restricted to named PC6 terminal state transitions and must not be relaxed to blindly certify this new Command. Follow-up #477 work must specify OD-5a destination state, idempotent domain application, terminal state/version plus immutable certification, and OS-kill windows around *each* publication/claim/ACK/Command/certificate commit, on SQLite and PostgreSQL with per-organization fencing and durable wait/retry. This PR does not certify a recurring production runner, alter the frozen Manufacturing/O2C/MRO v1 experiments, seal the prospective OD-5a execution freeze, or run the 17,920-case confirmatory DOE.
