# ADR-0002 — DomainReference owns experiment-domain semantics

- **Status:** Proposed
- **Date:** 2026-10-07
- **Decision owners:** SOSE maintainers
- **Related PRD(s):** `PRD-0001`
- **Related TRD(s):** `TRD-0001`
- **Supersedes:** none
- **Superseded by:** none

## Context

SOSE already has two distinct but related structures:

1. business domains/process canonicals, with domain definitions, specifications, runtime implementations, and evidence/maturity;
2. a synthetic organizational experiment implementation that currently embeds one reference process's parameter space, execution semantics, analytical truth, regimes, and reports.

Scaling the experiment by copying its modules into every domain would duplicate generic DOE/CRN/provenance logic. Moving all domain behavior into one generic experiment model would instead erase domain meaning and violate the existing framework-extraction rule.

A boundary is needed that allows the generic experiment framework to ask domain-specific questions without owning domain business semantics.

## Decision

Introduce a `DomainReference` experiment capability associated with an existing SOSE domain/catalog entry.

`DomainReference` owns the domain-specific semantics required by synthetic experiments:

- configurable exogenous parameter definitions;
- construction of a configured domain `ModelSpec`;
- domain-supported interventions;
- supported agency configurations/policies;
- execution adapter into the domain runtime;
- projection from raw domain execution into standard experiment observations;
- ground-truth claims;
- regime classification;
- provenance links to the domain/process specification and evidence.

The generic experiment framework owns orchestration:

- DOE;
- canonical world identity;
- replication;
- CRN comparison grouping;
- execution scheduling/persistence;
- generic observation/effect aggregation;
- claim eligibility enforcement;
- provenance manifests and standard report/data-product assembly.

The existing built-in domain/process catalog remains the authority for domain availability. `DomainReference` is additive experiment capability, not a replacement domain registry.

A domain can therefore be operationally available without being experiment-conformant.

The first implementation remains outside the kernel/stable public API until materially different domains demonstrate the contract. Stable promotion requires at least Manufacturing, Order-to-Cash, and MRO to pass the common conformance suite.

## Decision drivers

- avoid experiment-orchestration duplication across domains;
- preserve meaningful domain semantics;
- reuse the existing domain/process catalog;
- maintain a clear ownership boundary;
- make onboarding auditable and conformance-driven;
- delay premature kernel abstraction until cross-domain evidence exists.

## Consequences

### Positive

- new domains implement semantic adapters instead of copying DOE/report machinery;
- process canonicals remain authoritative for business behavior;
- generic experiment code cannot silently redefine domain rules;
- cross-domain conformance becomes possible;
- the current queue reference can migrate incrementally.

### Negative / trade-offs

- each domain must deliberately implement experiment capability;
- not every existing built-in domain will immediately be experiment-capable;
- the contract may need refinement after the first non-queue domains;
- temporary adapters/compatibility layers will exist during extraction.

### Constraints introduced

- generic experiment code must contain no domain-specific business concepts;
- a `DomainReference` must bind to an existing domain identity;
- experiment capability must not imply process-canonical maturity that has not been evidenced;
- domain-specific ground truth and regime logic stay domain-owned;
- stable/public promotion cannot occur based solely on the original queue reference.

## Alternatives considered

### Copy the current experiment per domain

Viable for rapid isolated demonstrations, but rejected because DOE, CRN, provenance, eligibility, persistence, and reporting would drift.

### Build a generic business-process meta-model for all domains

Rejected because it would flatten meaningful differences in workflows, resources, decisions, inventories, calendars, and reliability mechanics.

### Put all experiment methods directly on DomainDefinition/ProcessManifest

Rejected because operational domain registration, process maturity, and scientific experiment capability have different responsibilities and lifecycles.

### Promote the current queue experiment directly into the kernel

Rejected because one domain/reference is insufficient evidence for a durable core abstraction.

## Compatibility and migration

Existing domains and process manifests remain unchanged initially.

The current queue A0/A1 experiment becomes the first adapter/reference implementation.

Migration proceeds additively; old experiment entry points may delegate to the generic framework before eventual deprecation.

No persisted operational domain data is migrated by this decision.

## Verification

- current A0/A1 experiment reproduces its accepted semantic claims through the new contract;
- Manufacturing, O2C, and MRO implement the same interface without experiment-framework special cases;
- conformance asserts separation of domain semantics from generic orchestration;
- code review rejects domain-specific nouns/mechanics in generic framework modules;
- experiment capability does not alter existing process maturity automatically.

## References

- `docs/product/requirements/prd-0001-multidomain-synthetic-experiment-platform.md`
- `docs/technical/requirements/trd-0001-domain-neutral-reference-experiment-framework.md`
- `src/sose/examples/process_manifest.py`
- `docs/architecture/domain-frontier.md`
- `src/sose/organizational/synthetic_reference_experiment.py`
- `src/sose/organizational/synthetic_a1_experiment.py`

## Notes

After status becomes **Accepted**, substantive changes to this ownership boundary require a new ADR that supersedes this one. Lifecycle metadata may be updated according to ADR-0001.
