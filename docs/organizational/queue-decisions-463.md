# #463 — deterministic bounded OD-3 policy decisions

Two **real and different decision predicates**: adaptive follow-up contacts
oldest overdue eligible work; cherry picking prioritizes the shortest eligible
job, but an old waiting item receives forced priority after the configured
starvation limit. Cooldown, cost, null action, forbidden future observations,
owner segregation, replay conflicts and same-cost observational sham are
explicit. No process-global RNG; decisions are a pure function of the
supplied time, queue, observation identity and policy configuration.

This is **not #463 closure**: decisions currently persist an observational
DomainEvent without applying a physical queue mutation or debiting an actual
actor-time ledger. The next increment must introduce owner-authorized durable
queue/service action, policy activation and cost ledger, restart/writer fencing,
and paired CRN/sham/negative-outcome experiment on the #462 actor contract.
Do not confuse this with the historical A1 synthetic runner supporting only
BATCHING.

## Wave 1 durable effect/cost ledger (incremental)

Implemented in `sose.organizational.queue_policy_ledger`. What is enforced:

- **Anchor.** `seed_od3_queue` appends one `od3.queue_anchored` event (event
  id derived from the queue id) recording a SHA-256 digest of the full seeded
  items (every attribute, in order), paid time 0 and version 0. Seeding
  requires integer `service_us`, so the digest cannot drift through a
  float/JSON round-trip.
- **Linked effects.** Each `od3.queue_effect_applied` additionally records
  `previous_effect_id`, `before_items_digest`, `items_digest` (after),
  `applied_digest` (digest of the whole applied-observation map: observation
  ids, request ids and effect ids, after the effect) and the running
  `last_action_at`, beside the existing cumulative `paid_policy_us`
  and `queue_version`. The projection keeps the latest effect id in
  `ledger_head`. Each effect also appends one `od3.queue_version_claimed`
  slot event whose id is derived from the queue id and its version.
- **Admission check (incremental).** Before a *new* application, the runtime
  fetches by id only the anchor, the head effect, its paired
  `od3.policy_decided` event, its predecessor (effect or anchor) and two
  version slots. It requires that the head pairs with its decision
  (deterministic effect id, time, correlation, policy/action/selection/order/
  cost fields), that version and paid time advance by exactly one step and
  `cost_us` from the predecessor, that `before_items_digest` equals the
  predecessor's digest, and that the projection's paid time, version,
  observation count and applied-observation digest, last action/observation
  times, `updated_at`, item order and full items digest equal the head's (or the anchor's before the first
  application). A CONTACT head must also match the selected item's
  `last_contact_at`.
- **Latest, not just consistent.** The head must hold the slot of the
  projection's version and the next slot must be free. Re-saving an older but
  internally consistent snapshot (the seed state, an earlier effect, or a
  legacy state before or after adoption) is therefore rejected instead of
  forking the chain.
- **Owner scope.** Only events owned by the queue's owner (and, for
  anchor/effect/slot events, bound to this queue) count as evidence; another
  owner's event naming our queue under its own event id is ignored.
- **Cost.** The check does no log enumeration (`uow.events()`) and no deep
  copy of the log. The per-id lookup cost is the adapter's: the memory-derived
  unit of work (used by memory, SQLite and others) scans its event list in
  `get_event`.
- **Fail closed.** Any mismatch raises `ValueError("OD-3 charged queue ...")`
  inside the transaction; nothing is written. Replays of an already applied
  observation return the committed effect before this check, as before.

**Pre-ledger queues (compatibility).** A queue seeded by earlier code has no
anchor and no `ledger_head`. Its first application after upgrade *adopts*
the projection as it stands, including its applied-observation map
(`origin: "adopted"` anchor, only if the number
of applied observations equals the entity version) and verification starts
from that anchor; history before adoption is trusted, not verified. Once an
anchor exists, a projection without `ledger_head` is rejected (no downgrade).

**Not enforced.** Each admission verifies one link; earlier links were
verified when they were written, so the guarantee is inductive and assumes
events are append-only. A writer with public owner-scoped UoW access can
still forge a whole consistent decision/effect/slot set (#545). Event ids are
global per store and the memory-derived adapters do not reject a duplicate
id on append: an event of *any* owner placed at one of this queue's
deterministic ids (anchor, decision, effect or slot) makes seeding or the
next application fail closed (nothing is written). That store-level lockout
is left to #545, not fixed here. This is a **bounded queue-ledger
invariant**, not a real actor service reservation or conserved physical
worker time; durable actor-time debits in #462 must be approved before a
cross-service charge is considered scientifically authoritative. Tests cover
memory and SQLite. Do not close #463 from this slice alone.
