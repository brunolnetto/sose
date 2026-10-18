# ADR-0008 — Fail-closed admission of versioned organization composition

**Status:** Accepted — **bounded static admission v1 only** (#495; 2026-10-18), conditional on reviewed integration of #535 and independent acceptance of this ratification PR.  
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
7. A **separate static verifier**, `parse_static_calendar_source` and
   `verify_static_calendar_reference`, proves the bundled v1 UTC Monday-zero
   weekday schedule against canonical immutable JSON bytes and an independently
   pinned literal SHA-256. It rejects unsupported times/zones, unordered or
   overlapping windows, invalid schema/epoch/version and forged source claims.
   This is one reviewed positive **static** source, not an open calendar registry
   or authority to schedule actors/jobs. `validate_organization_admission`
   still rejects even a correctly pinned operational config as executable.
8. Reject every unaudited `PhysicalPoolRef` during static runtime metadata
   admission; names, ownership, capacity and actual shared instances must
   be proven by the authoritative resource registry/ledger (#477/#496),
   not by a caller-crafted graph.

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
- This accepted bounded static v1 contract refuses cycles without certified
  liveness, not implementing liveness. Epoch transitions, component/owner-qualified
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

## Static acceptance evidence and exact limits (#495)

The ratified **static subset** is supported by independent TDD and reviewed
code, without claiming that any runtime organization is admitted:

- #499: immutable `OrganizationDefinition`; process/entity owner, job/flow,
  boundary and physical-pool identities; deterministic fingerprints; rejection
  of dangling/self/duplicate topology.
- #510: `validate_organization_admission` matches real immutable domain
  EntityType registrations, audited v1 manifests, exact directional
  ingress/egress provenance and installed callable consumer handlers. Cyclic
  execution and unsupported versions fail closed.
- #512/#530: ownership review; structural `policy`, `calendar` and
  `cost_model` declarations, reference version/epoch/digest *syntax*, and
  fingerprint preservation for old configurations. No caller digest alone
  grants source-content truth.
- #534: caller-controlled physical pool identities fail closed; neither a
  pool name nor a projected capacity is evidence of an authoritative grant.
- #535: bundled **UTC Monday=0…Sunday=6** static source with positive and
  negative tests, canonical bytes, independently pinned SHA-256, typed
  closed-schema validation, and provenance rejection for forged declared
  version/source/ID. The verifier is independent of runtime admission;
  `validate_organization_admission` refuses operational references,
  even when their static source is positively verified.
- All evidence must satisfy exact-head Codex, Python matrix, PostgreSQL,
  chaos/coverage >=95% and three frozen historical replays. A CI failure
  or reviewer finding invalidates promotion of this ADR's implementation.

**Unsupported / explicitly deferred:** arbitrary policy or cost implementation;
caller-added calendar catalogs and arbitrary digests; positive runtime
policy/calendar/cost/physical-pool source attestation; live owner-qualified
component dispatch; writable organizational source state; runner/queue
construction; dynamic `config_epoch` transitions; global or cross-org
transaction/clock; resource grants, preemption and contested instance
occupancy. Those remain **#496**, **#477** and heterogeneous **#497**
and are not backfilled by this static decision.

The static `OrganizationDefinition` is a metadata declaration. The pure
`verify_static_calendar_reference` output is **not** accepted into the
executable runtime through `validate_organization_admission`: this
distinction is intentional, not an unresolved contradiction. This ADR
ratifies only a fail-closed contract that downstream work can depend on.

Neither accepting this ADR nor closing #495 is evidence that OD-5a #461
is scientifically admissible; it remains `NOT_ADMITTED` until its own
independently frozen physical/causal evidence clears #477.

Ratification of ADR-0008 does **not** ratify separate ADR-0009 on the
live owner-qualified runtime.
