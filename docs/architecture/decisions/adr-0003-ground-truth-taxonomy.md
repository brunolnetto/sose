# ADR-0003 — Use a typed taxonomy for synthetic ground truth

- **Status:** Proposed
- **Date:** 2026-10-07
- **Decision owners:** SOSE maintainers
- **Related PRD(s):** PRD-0001
- **Related TRD(s):** TRD-0001
- **Supersedes:** none
- **Superseded by:** none

## Context

The A0 reference experiment can use M/G/1 analytical truth, but many organizational domains do not have closed-form stationary solutions. Treating all references as analytical would produce false scientific precision.

## Decision

Every scientific claim binds to an explicit ground-truth kind:

- `ANALYTICAL`
- `MECHANISTIC`
- `INVARIANT`
- `REFERENCE_SIMULATION`
- `EMPIRICAL`

Ground-truth records also declare eligibility conditions, scope, and limitations.

Reports must not compare or aggregate claim strength as though these classes were equivalent.

## Decision drivers

- scientific honesty;
- domain portability;
- explicit falsification scope;
- separation of verification from validation.

## Consequences

### Positive

- domains can be verified without fake closed forms;
- report eligibility is machine-readable;
- empirical validation remains clearly separated.

### Negative / trade-offs

- report logic becomes more explicit/verbose;
- some domains need multiple ground-truth records for one experiment.

### Constraints introduced

- no unlabeled ground-truth metric;
- saturated/nonstationary cases cannot receive stationary analytical claims.

## Alternatives considered

### Analytical truth only

Rejected because most organizational processes lack tractable closed forms.

### Reference simulation for everything

Rejected because it discards stronger analytical/invariant knowledge where available.

## Compatibility and migration

Existing A0 analytical and A1 mechanistic classifications map naturally into the taxonomy.

## Verification

GroundTruth conformance tests verify class, eligibility, provenance, and non-overstatement rules.
