# OD-2 (#462) — bounded durable actor lifecycle

Status: **bounded implementation slice**, NOT full OD-2 admission, NOT OD-5b
scientific agency evidence. Related: [OD research #457](https://github.com/brunolnetto/sose/issues/457),
[OD-2 #462](https://github.com/brunolnetto/sose/issues/462).

## Implemented in this slice

- `OrganizationalActor` is a first-class, durable SOSE `Entity` with legal
  lifecycle `admitted → eligible ↔ active`,
  `eligible ↔ unavailable`, `eligible ↔ development`, and permitted exits
  only from non-active states. The StateChart, not a new global supervisor, is
  the legal transition authority.
- An explicit `owner_id`, immutable `model_spec_hash`, nonnegative integer
  `config_epoch`, actor ID and chronological UTC effective time belong to each
  actor; wrong owner, invalid epoch or source, time regression and invalid
  transitions fail closed before any durable effects.
- Each transition records elapsed **integer microseconds by prior state**,
  an explicit paid-transition microsecond cost and a durable causal Command →
  `entity.state_transition` event in the same authoritative transaction as
  the actor snapshot. The supplied `cause_id` is external observation
  provenance stored in the Command payload, not an invented internal parent.
- The same `cause_id` with identical arguments is idempotent; changed replay
  fails closed. An active actor cannot silently disappear: assigned work must
  be completed before exit or unavailability. Replacement requires an
  already-exited predecessor owned by the same organization; a predecessor-
  scoped PostgreSQL guard and one durable `replaced_by_actor_id` claim prevent
  two successors from forking one lineage. Only equal model hash/config epoch
  are admitted within this slice; configuration migration has its own gate.
  The predecessor's lifecycle/event history is never rewritten.
- Actor-time remains segregated by states; `UNAVAILABLE`, `eligible/IDLE`,
  `development` and `active` are distinct evidence classes. The
  `active` state is an actor's *claimed work* state, **not** a physical
  ResourceReservation.
- SQLite restart and in-memory tests cover temporal conservation, causality,
  idempotent replay, rollback of illegal transitions, actor independence and
  replacement identity.

## Outstanding #462 acceptance

1. Real worker/process death and simultaneous PostgreSQL actors attempting the
   same assignment; native resource fencing and physically unavailable actors.
2. Integration of `assigned_work_id` with live `ResourceReservation`,
   `ScheduledWork`, calendars and policy claims. No slot is allocated by
   setting actor state to `active` alone.
3. Actor-time accounting including explicit paid work, nonworking time,
   calendar eligibility and training across external policy/config epochs.
4. Exit/replacement when physical work is in-flight: explicit pause, transfer
   or cancellation with causal evidence and no leaked leases.
5. Independent versioned reproducible experiment/bundle and falsification of
   OD-2 effects. The actor mechanism is synthetic and not empirical validity.

Never make #462 a retroactive OD-5a #461 prerequisite. Frozen v1 source and
OD-5a scientific `NOT_ADMITTED` are unchanged.
