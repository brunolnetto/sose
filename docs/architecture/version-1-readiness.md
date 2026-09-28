# SOSE 1.0 readiness criteria

SOSE 1.0 should mean **compatibility confidence**, not feature completeness.

A library can always gain new adapters, domains, optimizations, and tooling after
1.0. The threshold is whether users can depend on the documented contract
without routine breaking changes.

## 1. Public API stability

Required evidence:

- `sose.api` has remained stable through at least one adoption-oriented minor
  release;
- public additions/removals are reviewed as compatibility changes;
- any replacement of a public symbol has exercised a documented deprecation
  cycle;
- public type/signature changes have executable anti-drift coverage;
- optional backends remain opt-in and do not make the core facade import-heavy.

1.0 promise:

- public API breaking changes require a major version;
- compatible additions may ship in minor versions;
- compatible fixes ship in patch versions.

## 2. Durable semantic compatibility

Required evidence:

- deterministic identity and event semantics remain stable across upgrades;
- ScheduledWork ownership/replay remains idempotent;
- Resource, Store, Container, and preemption terminal identities survive
  supported upgrades;
- restart equivalence remains green across supported persistence adapters;
- immutable occurrence/event history is not silently reinterpreted.

1.0 promise:

- supported upgrades preserve durable semantic truth or provide an explicit
  migration.

## 3. Persistence compatibility

Required evidence:

- schema and codec versions are explicit;
- every supported schema bump has a tested migration path;
- future schemas/codecs are rejected without mutation;
- at least one external persistence adapter has survived real upgrade/reopen
  usage;
- persistence compatibility policy covers module/type-path changes used by the
  durable codec;
- backup/restore and migration failure recovery are documented.

PostgreSQL is **not** itself a 1.0 requirement. A stable persistence contract is.

## 4. Release/distribution maturity

Required evidence:

- wheel and source distribution build on every change;
- built wheel installs in a clean environment;
- supported Python versions are explicit;
- release candidate metadata/tag/release-note checks are automated;
- published tags/artifacts are immutable;
- changelog and release closure documents describe actual behavior;
- at least two consecutive release cycles use the same release discipline.

## 5. Diagnostics and operability

Required evidence:

- users can inspect durable recovery position and subsystem ownership;
- consistency problems have stable machine-readable issue codes;
- support/debug guidance does not require backend-private state;
- common recovery failures have documented diagnostic paths;
- telemetry integrations, if added, observe rather than own semantic truth.

## 6. Performance characterization

Required evidence:

- benchmark workloads are reproducible and versioned;
- performance reports record environment metadata;
- key persistence/runtime cost centers are understood;
- any claimed performance targets are measured on controlled runners;
- optimizations do not weaken deterministic/restart semantics.

A specific throughput number is not a 1.0 gate unless the project publishes it
as an SLA.

## 7. Documentation/adoption

Required evidence:

- a new user can build a minimal restart-safe domain from public APIs;
- persistence setup/migration is documented;
- public vs advanced vs internal surfaces are documented;
- at least one external-user feedback cycle has informed docs/API decisions;
- References remain executable specifications rather than required reading for
  basic adoption.

## 8. Architecture restraint

Before 1.0, the project should re-evaluate abstractions that were deliberately
not promoted:

- Money;
- TemporalCapacity;
- QualifiedResource;
- generic inventory ledger.

They should enter core only if repeated independent domain/adoption evidence
shows a stable shared contract.

Absence of these abstractions is not a 1.0 blocker.

## 9. Proposed 1.0 release gate

A 1.0 release candidate should require:

1. all normal CI/release artifact gates green;
2. no unresolved public API migration;
3. no unresolved durable schema migration;
4. all supported persistence adapters pass conformance;
5. restart/hardening suites green;
6. release upgrade test from the previous supported minor version;
7. completed 1.0 compatibility/release document;
8. no known defect that can corrupt durable semantic truth.

## Decision rule

Do not release 1.0 because the feature list looks long.

Release 1.0 when:

> the cost of preserving the documented contract is understood and accepted,
> and users can upgrade without guessing what durable truth means.
