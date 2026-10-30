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

## Opt-in fixed-epoch availability window (#462 increment)

An enrolled actor may carry an immutable `ActorAvailabilityCalendar(version=1,
config_epoch, intervals)`. Its absolute UTC half-open intervals are sorted,
positive, nonoverlapping and source-digest bound to an independent
append-only actor enrollment `DomainEvent` committed in the same transaction
as the initial actor. The entity attribute is only a mutable projection and
cannot self-attest its hash or opt out by deleting the calendar field. `transition_actor(action="assign")` verifies the saved source
and checks eligibility at assignment **start** before creating any Command or
DomainEvent. The exact end boundary is not eligible; completing already
started work outside the window remains permitted for truthful accounting.
Malformed/falsified actor calendar snapshots fail closed, including after
SQLite restart. Existing actors without a calendar preserve historical
behavior rather than silently adopting a guessed working week.

This is an opt-in eligibility predicate, **not** a materialized actor
availability ledger, paid break accounting, physical capacity grant, guarantee
that service finishes before a closure, or prospectively admitted calendar
migration. The new test exercises boundary, corruption, epoch mismatch,
replay and restart on Memory/SQLite. PostgreSQL full CI must still pass.
Physical work and in-flight exit/cancel/transfer are independent #462
acceptance and remain OPEN. Do not infer real workplace schedules or OD-5b
causal effects from synthetic windows.

## Wave 1 — bounded causal time/cost projection guard (PR pending)

For **prospectively enrolled actors with `seed_actor(tracked_time=True)`**,
write a versioned `od2.actor_time_origin_recorded` admission event in the same
UoW as their initial entity. The event binds owner/model/epoch, replacement
identity and the original effective timestamp; rewriting `actor.created_at`
together with the admitted-time bucket can no longer pass the ledger audit.
Also commit append-only `od2.actor_nonwork_cost_caused` receipts for every
non-work native transition (even cost=0), preventing a paid
`begin_development` from being rewritten or downgraded by changing its
persisted Command.

Before a **new tracked** actor StateChart transition, reconstruct the actor's recorded
elapsed microseconds by original predecessor state and paid transition cost
from chronological native `entity.state_transition` events and their persisted
`Command` records. Require the projected per-state buckets, sum, final
state, last effective timestamp, charged cost, original cause identities, and
current assigned work to reconcile. For `assign`/`complete`, verify the
immutable `od2.actor_work_caused` source receipt in the same snapshot.

Memory and SQLite regression tests corrupt individual buckets (including
a *zero-sum reclassification*), paid cost, work identity, cause index, and
time while leaving real history untouched; the next attempted transition
must fail without appending an event/Command. A legal paid lifecycle and
SQLite restart are the positive control. Original idempotent replay-specific
checks remain in their existing order. Markerless/legacy actors keep their
previous reference-runtime semantics, but **cannot claim trusted actor-time
conservation** without a separately authorized, versioned cutover. Opt-in
records are still public-UoW consistency evidence, not protected issuer proof.

**Limit:** this detects inconsistent *mutable projections*; public writers
can potentially forge matching Command+event metadata. Only a separately
protected issuer and owner-fenced writer (#545) can establish source
authenticity. No physical `ResourceReservation`, worker-time consumption,
independently signed legacy cutover, or cancellation/preemption is asserted.
Scientific OD-5b admission remains separate.

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
