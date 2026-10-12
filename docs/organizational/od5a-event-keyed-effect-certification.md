# OD-5a — independent event-keyed effect materialization and certification

Parent tracker: #477; focused issue: #480. Stacked on #479 (durable typed physical event → claim → ACK → Command). No confirmatory design execution.

## PRD

Transport acceptance is not organizational state. Exactly one immutable OD-5a physical source event is allowed to become exactly one independently materialized *downstream* observation. Only the observation's terminal state/version, plus its accepted typed causal message, may produce an immutable business-effect certificate.

## TRD — authoritative operations

1. #479 atomically ACKs one boundary delivery and stages `od5a.stage_physical_completion.v1` with `entity_type=od5a_physical_observation`, `entity_id=<source DomainEvent.event_id>`, `correlation_id=<organization>` and the unique inbound boundary message as `causation_id`.
2. `apply_od5a_physical_observation` uses one authoritative UoW, or a PostgreSQL namespace-scoped boundary UoW, to require the exact pending Command, accepted delivery/consumption, immutable source event, model/admission hash, configuration epoch and matching physical instance interval. It creates one observation in state `applied`, with version >= 1 and a distinct immutable `od5a.observation_applied.v1` DomainEvent causally linked to the physical source. No resource reservation is manufactured retroactively.
3. A worker may die after the application transaction but before certification. Retrying application returns the unchanged observation and event; an unrelated organization/effect ID cannot overwrite them.
4. `BusinessEffectService.complete` recognizes **only** this explicit new intent/contract pair and revalidates physical provenance, exact observation attributes, applied event, accepted ACK and terminal entity version before atomically saving `BusinessEffectApplied` and deleting the original Command. A missing/forged observation or an ACK alone fails closed. All existing PC6 terminal intents/predicates remain untouched.
5. SQLite restart, stale/unknown Command, mismatched organization, counterfeit terminal state and independent organizations are tested. Future OS-process kill/PG/network fault cuts remain mandatory #477 follow-ups.

## ADR — causality, scope and non-goals

The authoritative physical operation is already an immutable `od5a.execution.v1` or `od5a.coordination.v1` event. The separate observation is not a replay of service work; it is a downstream semantic projection with independently attestable causal provenance and a versioned terminal receipt. A boundary ACK is transport truth, a Command is pending intent, `od5a_physical_observation.state=applied` is committed domain truth, and the `BusinessEffectApplied` is auditably certified evidence. They must not be conflated.

This PR proves neither a complete recurring production runner nor full OS-process fencing/fault-window matrix and does not certify the 17,920-run scientific DOE or change the v1 freezes. The independent admission validator #475 remains distinct from a prospectively signed/approved execution freeze.
