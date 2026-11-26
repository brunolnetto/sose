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

- **Verifying a history.** `TransitionAuthority.verify_history(events=...,
  entity=...)` authenticates one entity's whole receipt chain, given its transitions,
  their receipts and its attribute receipts (below) in commit order:
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
  the key id is `None`. Only receipts of the attributed machines' transitions are
  considered, so a stray receipt-named event elsewhere cannot deny the snapshot.
  `verify_history` also requires the chain to start at version zero and the stored
  `updated_at` to equal the last transition's time, as `validate_prestate` does
  before a dispatch.
- **What `authenticated` means.** The machine's history in this store is a complete,
  consistent chain of receipts signed by this key, and it ends at the stored entity.
  - **Not bound to a store.** Receipts do not name the store or organization, so a
    whole chain copied, with its entity, into another store authenticates there too.
    The same holds for a copied prefix together with the entity at that version.
  - **Rollback inside one store.** It cannot be done through the public API, which
    removes no events. The later transitions stay in the log and break the match.
  - **Same as `validate_prestate`.** It has the same property; this is not new.
- **Jobs.** `SimulationJob`, `PersistentJobRunner` and `build_attested_component_jobs`
  accept a `transition_authority`, and each tick's Engine issues receipts with it. It
  is refused together with a domain warehouse, because entity and event must commit in
  one store.

## Attested attribute-only saves (#494)

The transition receipt signs the attribute digest of the entity after each
transition. Domain code also saves entity attributes **outside** transitions, keeping
the same version and state. Before this increment that made the next attested
dispatch, and `verify_history`, fail closed, so 7 of the 28 catalog domains
(construction, field_service, hospitality, insurance, transit, warehouse_fulfillment,
warehouse_management) could not run attested. All 20 such save sites go through the
persistence the job hands its domain (`SimulationJob.domain_persistence`).

**Mechanism** (`sose.core.attestation_chain`):

- `Entity.attribute_revision` counts attested attribute-only saves within one
  version. A transition resets it to 0. The codec omits it when it is 0.
- Under a `transition_authority`, the job wraps its domain persistence in
  `AttributeAttestingPersistence`. A save that keeps version and state but changes
  attributes or `updated_at` bumps the revision and appends, in the same
  transaction, an `engine.entity_attributes_attested` receipt with id
  `(entity_type, entity_id, version, revision)`. It signs the state, the attribute
  digest and `updated_at`, and names the previous receipt of the entity (a
  transition receipt or an attribute receipt). Creations and Engine transitions pass
  through. A save that changes the state while keeping the version is refused. A
  save that also changes the version is not a transition the wrapper can tell from
  the Engine's, so it passes, and the next attested step or `verify_history` fails
  closed on it.
- A chain that starts at version 0 starts only from the chart's initial state: the
  job binds the domain registry to the wrapper each tick, and the first attribute
  save of a version-zero entity whose type has a StateChart **in the job's registry**
  is refused in any other state. A type that only another component registers is
  treated as chart-less here; another owner's Engine then refuses the foreign chain
  ("foreign owner") and the snapshot lists the entity as unattributed. Before the
  registry is bound (`initialize`/seed, and while a process builds its first tick's
  runtime), a save of an entity still at version 0,
  revision 0 is stored unsigned, like a creation: nothing is signed, and the first
  attested step checks the root.
- `verify_history` also requires every receipt of a chain to be issued for one owner,
  and each attribute receipt to name the receipt its own (version, revision)
  implies, not only the previous one in the list.
- The head of an entity's chain is found from the stored entity: revision 0 is the
  transition receipt of its version, revision `r > 0` the attribute receipt
  `(version, r)`. An existing receipt for the next revision or the next version
  means the stored entity was rolled back, and the next attested step fails closed.
  A transition after attribute saves names the last attribute receipt as its
  predecessor (`predecessor_revision` claim, set only when non-zero, so older
  receipts keep their claims).
- `verify_history(events=..., entity=...)` walks transitions, their receipts and
  attribute receipts in commit order, and the last receipt must describe the stored
  entity. The organization snapshot feeds each attributed machine's chain events.

**Measured.** Every catalog domain run as an attested `SimulationJob` (seed 3, 30
triggers) now verifies: 28 of 28 (`test_every_catalog_domain_is_attestable`, marked
slow). Without an authority, a one-off comparison (not a test) found the stored
events, entities and checkpoints of all 28 domains (seed 3, 10 triggers)
byte-identical to `main` before this change; the test suite checks one domain.

**Limits.** A save made through the raw persistence, bypassing the wrapper, is not
attested and is detected only at verification ("contradicts the stored entity").
`authenticated` is still not bound to the store (see above).

## Rollback and uncertain COMMIT on an attested ship (#494)

`tests/e2e/sose/persistence/test_postgres_attested_uncertain_commit.py` runs a
durable, scheduled `ship` through an attested Engine in a separate OS worker on real
PostgreSQL. The parent never trusts the worker's exit code; it reads the outcome on
a fresh direct connection.

- **Injected rollback:** the worker raises inside the ship transaction after the
  transition, event and receipt rows were written. Nothing is left: the entity is
  still `packed` at version 3, there is no ship transition and no receipt, and the
  scheduled Command is still pending. Its retry ships exactly once.
- **Lost COMMIT reply:** the #584 wire proxy forwards the COMMIT, witnesses the
  backend's CommandComplete and drops it. The worker fails with a transport error.
  Everything is there exactly once (entity, transition, receipt, consumed Command)
  and `verify_history` passes. Retrying the stale callback returns `False`, and a
  second `ship` is refused, so neither adds a transition or a receipt.

**Mutant check.** An Engine that commits the receipt in its own transaction after
the transition fails the rollback test (a transition with no receipt survives). It
passes the lost-reply test: after a successful COMMIT both transactions are durable.
That test proves "no duplicate on retry", and that the Command is not consumed in a
later transaction than the ship (an Engine doing so fails it; one consuming it in an
earlier transaction fails the rollback test instead). It does not prove that
transition and receipt are indivisible.

Not covered: a SIGKILL of the worker between statements (a killed connection rolls
back like the injected failure, but this is not tested here), PostgreSQL writer-role
and key isolation, and the attribute-receipt path under the same faults.

## Ed25519 receipts: verifiers hold only the public key (#494)

With `TransitionAuthority` (HMAC-SHA256) the same secret signs and verifies, so any
party able to check a receipt can also forge one. #545 asks for "a signature that only
the authorized Engine can produce and downstream validators can verify from a
separately managed root". `sose.core.ed25519_authority` provides it:

- `Ed25519TransitionAuthority(issuer_id, key_id, private_key)` signs transition and
  attribute receipts with a raw 32-byte Ed25519 private key. Its `verifier()` returns
  the public half.
- `Ed25519TransitionVerifier(issuer_id, key_id, public_key)` authenticates receipts
  exactly as the issuer does (`verify`, `verify_history`, `snapshot_organization`),
  from the raw 32-byte public key. It cannot issue receipts: an Engine or job
  configured with it raises `PermissionError` at the first transition or attribute
  save, inside the transaction, so nothing is written.
- Ed25519 receipts carry `"algorithm": "ed25519"` and a 128-hex-digit signature. An
  HMAC authority refuses them, and an Ed25519 verifier refuses HMAC receipts
  ("signature missing"). HMAC receipts are byte-for-byte unchanged.
- The signing schemes share `ReceiptAuthority`, which holds the chain logic; each
  scheme implements only how to sign and check a signature. Pure claim checks moved
  to `sose.core.receipt_claims` and are re-exported from `transition_authority`.
- Needs the optional `cryptography` package (`pip install sose[attestation]`; it is in
  the `dev` extra). Without it, constructing an Ed25519 class names the extra.

**Tests.** `tests/integration/sose/composition/test_ed25519_receipts.py`:
- a `warehouse_fulfillment` job signed with Ed25519, with transition and attribute
  receipts, verifies from the public key alone;
- a verifier configured as the job's authority writes no receipt and no entity change;
- receipts signed by another key with the same issuer and key ids fail;
- HMAC and Ed25519 receipts are not interchangeable;
- tampering with a claim, or stripping `algorithm`, fails;
- the organization snapshot authenticates with the verifier.

`tests/e2e/sose/persistence/test_postgres_ed25519_receipts.py`: on real PostgreSQL, a
fresh OS process given only the public key verifies a signed `ship` chain. It refuses
a chain a public writer signed with its own Ed25519 key, and cannot sign itself.

**Limits.** This settles who can *verify* without being able to *issue*. Key custody
and rotation are deployment concerns: the private key still lives in the Engine
process, and a process holding it is trusted. Rotation, meaning more than one
accepted key, is not implemented; a verifier accepts exactly one `(issuer_id,
key_id, public_key)`. A PostgreSQL writer role that stops public writers from
appending receipt rows is not implemented either. Such rows are rejected at
verification, which is the integrity property, but they can still deny availability.
