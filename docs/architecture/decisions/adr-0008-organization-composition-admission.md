# ADR-0008 — Fail-closed admission of versioned organization composition

**Status:** Proposed (requires independent architecture review; #495)  
**Scope:** catalog-to-runtime binding only; no executor, no scientific freeze change.

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
- Enforcing liveness on legal typed feedback, epoch transition semantics,
  executable projections, concurrent shared pools and O2C+MRO recovery
  remains part of #495/#496/#497. The current test slice alone does not
  justify closing #495 or claiming cross-domain scientific conformance.
- OD-5a #461 retains its own preregistration and #477 physical adapters
  as admission gates, independently of #496/#497. The historical v1
  Manufacturing/O2C/MRO freezes are immutable.

## Alternatives rejected

- Treating arbitrary strings as installed runtime contracts.
- Assuming a PC5 domain is automatically PC6 executable.
- Silently coercing contract versions, flow IDs or owner identities.
- Introducing a generic enterprise kernel primitive before two domain
  implementations demonstrate a real shared abstraction.

## Evidence required for ratification

Exact-head Python matrix, PostgreSQL, coverage ≥95%, historical v1 replay
3/3, negative registry/manifest/chart/version/cycle tests, no unresolved
independent review findings, and audited production ingress/egress
manifest entries, including per-version source provenance. Keep this ADR **Proposed** until those gates and
the remaining #495 lifetime/epoch acceptance are reviewed.
