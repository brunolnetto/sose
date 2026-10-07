# ADR-0003 — Use typed ground-truth claims instead of one universal truth model

- **Status:** Accepted
- **Date:** 2026-10-07
- **Decision owners:** SOSE maintainers
- **Related PRD(s):** `PRD-0001`
- **Related TRD(s):** `TRD-0001`
- **Supersedes:** none
- **Superseded by:** none

## Context

The current synthetic reference experiment can compare stable queueing worlds with an analytical M/G/1 reference. That is unusually convenient.

The target SOSE domain catalog includes systems such as Manufacturing, Order-to-Cash, MRO, Insurance, Airports, Credit & Loans, Telecommunications, Payments, and Warehouse processes. Many of these do not have one closed-form analytical solution for the metrics or regimes of interest.

Requiring analytical truth everywhere would either exclude useful domains or encourage false analytical simplifications. At the other extreme, calling any simulator output "ground truth" would weaken falsifiability and blur synthetic verification with empirical validation.

SOSE needs a typed claim system that records what kind of reference supports each assertion and under which assumptions the comparison is eligible.

## Decision

SOSE will represent ground truth as one or more explicit `GroundTruthClaim` records, each classified by `GroundTruthKind`.

The initial taxonomy is:

### ANALYTICAL

A closed-form or otherwise independently calculable mathematical reference.

Examples:

- M/M/1, M/G/1, M/M/c;
- Little's Law relationships;
- Jackson-network quantities under their assumptions;
- exact allocation/conservation formulas.

An analytical claim must record its mathematical assumptions and becomes ineligible when those assumptions are violated.

### MECHANISTIC

A result implied directly by configured generating mechanics, without using realized outputs to define the reference.

Examples:

- offered-load stability boundary from configured rates/capacity;
- a deterministic policy trigger and allowed action envelope;
- structural reachability implied by configured topology.

Mechanistic truth may classify a regime even when no closed-form performance metric exists.

### INVARIANT

A property that must hold for every valid execution.

Examples:

- conservation of money/material/items;
- ledger completeness/exclusivity;
- precedence legality;
- non-negative inventory;
- deterministic replay equality;
- monotonic or bounded relationships where formally justified.

Invariant claims normally produce pass/fail or bounded results rather than point estimates.

### REFERENCE_SIMULATION

A simpler or independently implemented reference simulator with deliberately narrower semantics than the system under test.

This kind is allowed only when:

- the reference implementation is independent enough to avoid tautological self-comparison;
- its scope/assumptions are explicit;
- seeds and provenance are recorded;
- the claim is labeled simulation-based rather than analytical.

### EMPIRICAL

Observed external data used for calibration, validation, or held-out prediction.

Empirical claims belong to L5/L6 validation and are not required for L0–L4 synthetic verification.

They must not be silently mixed into synthetic ground-truth scores.

## Decision drivers

- preserve falsifiability across heterogeneous organizations;
- avoid forcing every domain into queueing closed forms;
- distinguish mechanism knowledge from empirical evidence;
- make claim eligibility explicit;
- prevent a universal composite score from hiding incomparable evidence;
- support cross-domain reporting without pretending all truths are of equal type.

## Consequences

### Positive

- domains can use the strongest available reference without overstating certainty;
- reports can explain exactly what was verified;
- analytical and mechanistic claims can coexist;
- invariants become first-class scientific/engineering evidence;
- empirical validation remains clearly separated.

### Negative / trade-offs

- reports become more structured and less reducible to one headline number;
- reviewers must inspect claim type and eligibility;
- reference-simulation independence requires care;
- cross-domain aggregation must operate over comparable claim classes rather than arbitrary metrics.

### Constraints introduced

Every ground-truth claim must include:

- stable claim identifier;
- `GroundTruthKind`;
- target (metric, regime, invariant, or relation);
- expected value/class/bound or predicate;
- assumptions;
- eligibility rule;
- provenance/reference;
- comparison/falsification rule where applicable.

A result is not allowed to claim analytical agreement if its analytical assumptions are false.

Saturated/non-stationary worlds must not receive stationary mean comparisons merely because a finite-horizon simulation produced a finite number.

Realized endogenous outputs cannot define a mechanistic regime label that is later presented as independently recovered ground truth.

Cross-domain reports may summarize counts/rates only among explicitly comparable eligible claims.

For an official experiment, the intended claims, their ground-truth kinds, assumptions, eligibility rules, metrics, and falsification criteria must be frozen in the accepted domain-specific preregistration protocol before result execution/inspection. Corrections after a discovered implementation/protocol defect require explicit versioned deviation documentation and a complete rerun; they must not silently retune claims against observed results.

## Alternatives considered

### Require analytical truth

Rejected because it excludes or distorts many target domains.

### Treat configured model parameters as one untyped ground truth blob

Rejected because parameter truth, regime truth, invariants, and analytical metric truth have different semantics.

### Use a trusted reference simulator for all domains

Rejected because it creates a second engine and can become circular or equally complex.

### Mix empirical observations into the same truth taxonomy without distinction

Rejected because synthetic verification and empirical validation answer different scientific questions.

## Compatibility and migration

The existing queueing reference becomes `ANALYTICAL` for stationary performance claims and `MECHANISTIC` for configured stability/regime claims.

Existing ledger/conservation tests map naturally to `INVARIANT`.

The prospective GitHub empirical study, if continued, is classified `EMPIRICAL` and remains separate from synthetic experiment promotion.

No current evidence is invalidated merely by adding the taxonomy; persisted reports may be versioned when migrated to typed claims.

## Verification

Conformance tests must prove:

- every claim has kind, assumptions, eligibility, and provenance;
- ineligible claims cannot contribute to their corresponding agreement statistic;
- stationary analytical claims reject saturated/non-stationary cases;
- mechanistic classifiers consume configured mechanics rather than realized outcome metrics;
- empirical evidence is not required for synthetic L0–L4 promotion;
- reference-simulation claims identify an independent reference implementation and seed/provenance.

## References

- `docs/product/requirements/prd-0001-multidomain-synthetic-experiment-platform.md`
- `docs/technical/requirements/trd-0001-domain-neutral-reference-experiment-framework.md`
- `docs/organizational/evidence/synthetic-reference-v1/RESULT.md`
- `docs/organizational/evidence/synthetic-a1-reference-v1/RESULT.md`

## Notes

This ADR standardizes the semantics of evidence, not a universal score. Substantive changes require a superseding ADR after acceptance.
