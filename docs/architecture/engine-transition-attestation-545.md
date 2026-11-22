# #545 — Engine transition attestation, bounded first increment

This is an **opt-in** signing root, not a general production security boundary.

A caller with public UnitOfWork write access can forge a matching Entity and
DomainEvent. This new Engine mode issues an HMAC-authenticated DomainEvent after
a real StateChart listener transition, **in the same owner-scoped transaction**
as the entity mutation and transition event. The attestation binds source and
target versions/states, command, event, owner, tick, time and correlation, with
a verifier key provided independently of the persisted entity/event records.

The key must be provisioned by a separate trusted service/operator to the
Engine and verifier, not derived from model metadata or stored in the UoW.
Anyone with Python-process memory access to the key remains trusted; do not
expose it in persisted config, JSON, logs or to an untrusted plugin.

Receipt ids are owner-independent (`entity_type`, `entity_id`, version), matching
the entity identity space. Once any owner has attested a version of an entity, no
other owner can advance that entity under an authority: there is no ownership
hand-off and no re-anchoring between owners, nor for entities whose history
predates the authority. Such dispatches fail closed.

This increment does **not** certify #545: key-rotation and writer-role separation,
stale PostgreSQL fencing epochs, durable cross-worker issuer authority,
receipt transport/verification in executable organization #496, physical
reservations and downstream Command->DomainEvent causal equivalence still need
independent adversarial PostgreSQL tests. Never merge closed #543 wholesale or
treat this receipt as a completed organizational execution certificate.

## Verifying receipts in the organization snapshot (#494)

- **Verifying a history.** `TransitionAuthority.verify_history(transitions=...,
  receipts_by_source=..., entity=...)` authenticates one entity's whole transition
  history:
  - each transition needs its own receipt;
  - each receipt must name the previous one, and the first names none;
  - the last receipt must describe the stored entity (version, state and attribute
    digest).

  A forged, unsigned, reordered or missing step, or a projection rewritten after the
  last receipt, fails closed.
- **Snapshot.** `snapshot_organization(..., transition_authority=...)` runs that check
  for every attributed machine and marks it `authenticated`. The snapshot records the
  verifying `authenticated_key_id`. Two receipts for one transition also fail closed.
  Without an authority the snapshot is unchanged: machines are not authenticated and
  the key id is `None`.
- **Jobs.** `SimulationJob`, `PersistentJobRunner` and `build_attested_component_jobs`
  accept a `transition_authority`, and each tick's Engine issues receipts with it. It
  is refused together with a domain warehouse, because entity and event must commit in
  one store.

## Finding: the catalog's warehouse domains are not attestable yet

The receipt signs the attribute digest of the entity after each transition. Both
`warehouse_fulfillment` and `warehouse_management` also save entity attributes
**outside** transitions, keeping the same version and state. The next attested
dispatch on such an entity then fails closed ("receipt contradicts terminal
projection"). The write sites found by tracing a composed run are:

- `warehouse_fulfillment`: `allocation.py`, `service_tasks.py`, `handling.py`, through
  `definition.py` and `simulation.py`;
- `warehouse_management`: `records.py`, `simulation.py`, `unloading.py`, through
  `definition.py`.

Making these domains attestable means attesting attribute updates too: Engine-issued
receipts for attribute-only saves, chained with the transition receipts. That is the
next increment, and its design is still to be agreed. Until then, the receipt checks
in the snapshot are exercised with an attested Engine driving the order chart
directly, and with `tutorial_job` for the job plumbing.

