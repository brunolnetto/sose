# ADR-0008 — Fail-closed admission of versioned organization composition

**Status:** Proposed — **bounded static admission v1** (#495; 2026-10-18). Ratification blocked on static calendar schema and independently auditable reference/provenance decisions.  
**Scope:** validated catalog-to-runtime *metadata* binding only; no runnable composed organization, dynamic configuration admission, new pool grant, or scientific freeze change.

## Context

A syntactically valid `OrganizationDefinition` (#499) is only a description,
not proof that either participating process can actually produce or consume
the declared contract. The first implementation accepted chart names,
boundary strings and version numbers without checking real execution
capabilities. This was a dangerous admission boundary for #496/#497.

## Decision

1. Keep each process's immutable entity/StateChart owner and local clock.
   Organization identity and flow correlation identity remain distinct.
2. Admit a declared StateChart only against **explicit, immutable EntityType
   registrations shared by the real domain runtime and its DomainDefinition**.
   Never call `default_config`, `build_runtime`, or `seed` for discovery: their
   side effects are unconstrained. Only **process v1** is admissible against the
   current domain-keyed audited manifests. Even when a DomainDefinition and
   component both declare v2, admission refuses to inherit v1 evidence until
   manifests have an independently certified process-version identity.
3. Require an audited `ProcessManifest` for **both** components; each exact
   direction/name/version needs its own `contract_provenance` paths. The producer
   must explicitly declare the versioned egress contract **with** audited
   `ProcessEvidence.EGRESS_CONTRACTS`, the consumer the same versioned ingress
   **with** audited `ProcessEvidence.INGRESS_CONTRACTS`, including provenance;
   its actual `BoundaryConsumerRegistry` must have a handler installed for
   that exact **source/destination/contract/version** tuple with a callable
   handler, rejected otherwise during registry registration. Destination-only
   generic registrations do not prove composition admission.
   Unknown names, unsupported versions, missing manifests and unregistered
   handlers fail closed. Version 2 does not silently fall back to version 1.
4. Cyclic cross-component dependencies are conservatively **not admitted**
   in v1 without a separately certified typed cycle/liveness policy.
   Feedback is not globally illegal, but a structural graph alone provides
   neither bounded queue semantics nor a deadlock/recovery proof.
5. Organization snapshots are derived from constituent durable truth.
   Labels such as `capacity_constrained` are projections, never another
   writable business StateChart. A governance StateChart is optional and
   must have independent, attributable authority-changing transitions.
6. `config_epoch` and definition fingerprint express an immutable
   configuration identity. Executable jobs in #496 must bind to both and
   reject stale epochs/fenced owners; this admission slice does not yet
   implement job transitions or epoch migration.

## Consequences and limitations

- Reuse `DomainCatalog`, `DomainRegistry`, `ProcessManifest` and
  `BoundaryConsumerRegistry`. Do not add a second contract registry, legacy
  owner fallback, global clock, cross-domain COMMIT or writable aggregate.
- Cross-domain bindings currently register handlers by **source and
  destination domain**, not by component ID or physical owner. The admission
  gate therefore rejects a domain reused by multiple components whenever
  that domain participates in a boundary. Disconnected same-domain
  components remain structurally valid. Do not claim a composed owner-unique
  runtime until typed messages, admission, and dispatch are all explicitly
  component/owner qualified and independently conformance tested.
- The current built-in Warehouse Management manifest does **not** yet claim
  `warehouse.inventory_reservation_requested.v1` as an audited ingress.
  An actual WF→WM binder must therefore fail closed until the claim is
  audited and the matching real handler is registered. A unit test can
  supply an explicit hypothetical manifest as a pure validation fixture;
  that fixture is **not** a production maturity upgrade.
- This v1 proposal refuses cycles without certified liveness,
  not implementing liveness. Epoch transitions, component/owner-qualified
  dispatch, executable projections and configuration migration belong to
  #496; shared physical grants and certification remain #477, and real
  O2C+MRO recovery remains #497. None is silently certified by #495.
- OD-5a #461 retains its own preregistration and #477 physical adapters
  as admission gates, independently of #496/#497. The historical v1
  Manufacturing/O2C/MRO freezes are immutable.

## Alternatives rejected

- Treating arbitrary strings as installed runtime contracts.
- Assuming a PC5 domain is automatically PC6 executable.
- Silently coercing contract versions, flow IDs or owner identities.
- Introducing a generic enterprise kernel primitive before two domain
  implementations demonstrate a real shared abstraction.

## Evidence assembled and remaining static-admission gates (#495)

- #499: immutable `OrganizationDefinition`, component/owner/job identities,
  typed boundaries, external pool references, deterministic fingerprints and
  negative construction tests.
- #510: `validate_organization_admission` checks actual `DomainCatalog`,
  registered StateChart classes, independently audited v1 manifests, exact
  ingress/egress provenance and callable, source-qualified consumer handlers;
  unsupported versions and unproved feedback cycles fail closed.
- #512: contract/ADR ownership audit and explicit distinction between
  supported production ingress and hypothetical validation fixtures.
- #530: versioned typed policy/calendar/cost *references* with caller-declared
  SHA-256 **syntax** (not independently reproducible content provenance),
  epoch consistency and backwards-compatible empty v1 fingerprints.
  References are **rejected at admission**, not treated as installed semantics.
- Those PRs passed their exact-head required Python, real PostgreSQL, coverage
  >=95%, chaos and three pinned historical replays with Codex findings
  addressed before merge. The subsequent main CI through #533 is green.
  Contract tests live in `tests/unit/sose/composition/`.

**Remaining #495 gates — no premature issue closure:**

1. Define and test a **static calendar payload/schema** capable of rejecting
   invalid schedule windows, contradictory versions/epochs and unverifiable
   source claims. Today a calendar reference is an unverified digest string;
   refusing every reference is safe but is not positive schema validation.
2. The actual physical pool/owner/capacity cannot be attested by a caller's
   `PhysicalPoolRef`. The static validator now fails closed on **all** pool
   references until a trusted physical-authority lookup exists (#477/#496).
3. Explicitly review the supported positive static-admission subset and the
   negative shape of unsupported policy/cost/calendar references against the
   original PRD/TRD. In particular do not call a typed digest **proof** that
   its referenced source bytes were independently reviewed.

**Future operational acceptance:** executable policy/calendars/costs, dynamic
`config_epoch` migration, job creation, owner-qualified dispatch, durable
projections and recovery belong to #496; temporal physical authority and
joint commit to #477; O2C/MRO generality to #497. No `OrganizationInstance`
API, physical capacity, science or actor availability is admitted by this
ADR. Keep #495 **OPEN** until the static schema and ratification tests above
are actually reviewed. Other issues remain open for their own runtime gates.

Ratification of this ADR is **not** ratification of distinct ADR-0009,
which governs the live owner-qualified runtime.
