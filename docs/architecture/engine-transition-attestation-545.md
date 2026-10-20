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

This increment does **not** certify #545: key-rotation and writer-role separation,
stale PostgreSQL fencing epochs, durable cross-worker issuer authority,
receipt transport/verification in executable organization #496, physical
reservations and downstream Command->DomainEvent causal equivalence still need
independent adversarial PostgreSQL tests. Never merge closed #543 wholesale or
treat this receipt as a completed organizational execution certificate.
