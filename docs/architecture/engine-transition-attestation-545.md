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
  ("signature missing"). Only lower-case hex is accepted, so an upper-cased copy of a
  receipt does not verify. HMAC receipts are unchanged: an independent review compared
  the 82 receipt payloads of `warehouse_fulfillment` (seed 3, 12 triggers) on `main`
  and on this change and found them identical (a one-off check, not a test).
- The signing schemes share `ReceiptAuthority`, which holds the chain logic; each
  scheme implements only how to sign and check a signature. Pure claim checks moved
  to `sose.core.receipt_claims` and are re-exported from `transition_authority`.
- Needs the optional `cryptography` package (`pip install sose[attestation]`; it is in
  the `dev` extra). Without it, constructing an Ed25519 class names the extra.

**Tests.** `tests/integration/sose/composition/test_ed25519_receipts.py`:
- a `warehouse_fulfillment` job signed with Ed25519, with transition and attribute
  receipts, verifies from the public key alone;
- a verifier configured as the job's authority writes no receipt and no entity change,
  on the transition path (Memory) and the attribute path (Memory, SQLite);
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

## Cancel versus claim of the same attested work (#494, #578)

`tests/e2e/sose/persistence/test_postgres_cancel_vs_dispatch.py` races two OS processes
on one durable scheduled `ship` on real PostgreSQL. One process claims it with an
attested `Engine.dispatch_scheduled`, and the other withdraws it with
`DurableScheduler.cancel`. The interleaving is forced in both orders. The first
process stops inside its writing transaction, after taking the per-owner advisory
lock and reading the work. The parent confirms in `pg_stat_activity` that the other
process is waiting on that lock, and only then lets the first commit.

- **Claim first:** shipped exactly once, with one transition and one receipt. The
  transition's `causation_id` and the receipt's `command_id` are the `ship` Command,
  the receipt names the transition, and `verify_history` passes. `cancel` returns
  `False`.
- **Cancel first:** still `packed`, with no ship transition and no receipt.
  `dispatch_scheduled` returns `False`.
- **Either way:** the work and its Command are gone.

**RED mutant.** A cancel whose transaction skips the owner lock does not wait. Both
sides commit: the cancel reports `True`, yet the order shipped and holds a receipt.
That is a ghost withdraw, and the test asserts it to show the oracle sees it.

What the test proves: the per-owner `pg_advisory_xact_lock` taken by
`transaction(owner_id=...)` serializes a withdraw against a claim of the same owner's
work. Not covered: the same race across *different* owners (the lock is per owner),
resource release intents, and boundary-message ACK (#496).

## W2 RED mutant matrix (#494, #578)

`tests/integration/sose/composition/test_w2_red_mutant_matrix.py` pairs each #578
mutant with the oracle that must catch it. The oracle passes on the real code (GREEN)
and fails with its own assertion message when the mutant is applied (RED). It runs
in-process on Memory, so every mutant is exercised on every CI run; the PostgreSQL
versions of these properties are in the e2e tests named below.

| #578 mutant | Mutant applied | Oracle (PostgreSQL counterpart) |
|---|---|---|
| ignored picker capacity=2 | the SimPy resource is created with capacity 1 | two pick tasks run in parallel with two reservations (W1 capacity tests) |
| premature consumption | work and Command are deleted in their own transaction before the claim | a claim that fails after its transition leaves the Command pending (`test_postgres_attested_uncertain_commit`) |
| forged owner source | HMAC `TransitionAuthority._valid` accepts any signature | a chain signed by a foreign key (same issuer and key ids) is refused (`test_postgres_trusted_transition_receipts`, wrong root: "signature invalid") |
| stale owner | the writer-epoch check is a no-op | a superseded writer cannot dispatch and leaves no receipt (`test_postgres_trusted_transition_receipts`, stale issuer after takeover; it fences `start_pick`) |
| duplicate post-COMMIT effect | a consumed callback is re-inserted and executed | retrying a committed claim adds no transition and no receipt (`test_postgres_attested_uncertain_commit`, lost COMMIT reply) |

The matrix shows these oracles are sensitive to these defects. It does not run the
PostgreSQL tests under the mutants. Two rows are narrower than their labels:

- "forged owner source" models a foreign signing key being accepted, not a forged
  owner or issuer field;
- the Memory oracle's duplicate-effect retry runs without an authority (the PostgreSQL
  test retries with one), because on the attested path
  the chart already refuses a second `ship`, which would mask a missing
  consumed-callback guard.

## A contended queue keeps its order across restarts (#494, #477)

`tests/integration/sose/composition/test_fair_queue_across_restart.py` sets up one
capacity-1 pool with five waiters of mixed priority, on Memory, SQLite and
PostgreSQL. In the restarted run, every grant decision is made by a fresh
`DurableResourceManager` and SimPy backend rebuilt from durable state, after
reopening the SQLite file or PostgreSQL connection (Memory keeps its state object).

- The restarted run grants in the same order as the continuous run: the first
  arrival, then by priority, with ties in arrival order (`a, c, e, b, d`).
- Every waiter is served, and the pool never holds two reservations at once.

This holds because the order lives in the durable `ResourceDemand`, which is ordered
by `(priority, sequence)` and which every adapter returns sorted, and because SimPy's
`PriorityResource` keeps arrival order among equal priorities. A RED mutant that
rebuilds the waiters in reverse serves `a, e, c, d, b`, and the test asserts it.

**Limits.** "No starvation" here means every queued waiter is eventually served once
arrivals stop. With strict priorities and no aging, a steady stream of
higher-priority arrivals can still delay a lower-priority waiter indefinitely. That is
the configured policy, not a defect, and no aging policy exists. This test does not
cover preemption. Temporal ledger preemption surviving a reopen is covered by
`test_postgres_temporal_resource_ledger`; the preemptive queue rebuild
(`core/preemption_rebuild.py`) is not covered here.

## Lost COMMIT reply at each boundary step (#494, #477)

`tests/e2e/sose/persistence/test_postgres_boundary_lost_commit_reply.py` runs
`BoundaryService.publish`, `claim_next` and `consume` in a worker over the #584 wire
proxy. The proxy is armed inside one stage's write transaction, after its rows were
written. It forwards that COMMIT, sees the backend's CommandComplete, and drops the
reply, so the worker fails with a transport error. The parent then reads the outcome
on a fresh direct connection and retries as a caller would:

- **publish:** one message and one PENDING delivery. A retried publish returns the
  same delivery and adds nothing.
- **claim:** one CLAIMED delivery at epoch 1, which nobody else can claim before the
  lease expires. After expiry a new claim gets epoch 2, the unconfirmed epoch-1 lease
  is refused at ACK (`StaleBoundaryClaimError`), and the new lease ACKs.
- **ACK:** one consumption, the delivery CONSUMED and the Command staged once. A retry
  returns that consumption, and a counting handler shows zero further calls. The
  CONSUMED short-circuit answers before any lease check, so the lease used does not
  matter.

Each case ends with exactly one message, delivery, consumption and Command. The test
was repeated 3 times (9/9). As a one-off mutant check, not kept as a test, a `consume`
that is not idempotent on an already-CONSUMED delivery made the ACK case fail.

Other PostgreSQL tests already cover the remaining #477 kill points:
- an OS kill between steps and a rollback inside the ACK:
  `test_postgres_pc6_full_real_domain_recovery` and
  `test_postgres_pc6_uncertain_ack_commit`;
- a death after a reservation committed:
  `test_postgres_pc6_federated_recovery` (`after_booking`);
- a death before or after the certified-effect commit, and a lost certified-effect
  reply: `test_postgres_pc6_uncertain_effect_commit`.

Not covered:
- a rollback inside the publish transaction;
- a death inside the physical reservation transaction of the PC6 chain. Its OD-5a
  analogue (`pg_terminate_backend`) is
  `test_postgres_od5a_physical_backend_loss`.
