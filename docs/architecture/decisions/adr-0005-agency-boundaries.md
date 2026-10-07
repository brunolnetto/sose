# ADR-0005 — Agency levels have explicit ownership boundaries

- **Status:** Proposed
- **Date:** 2026-10-07
- **Decision owners:** SOSE maintainers
- **Related PRD(s):** PRD-0001
- **Related TRD(s):** TRD-0001
- **Supersedes:** none
- **Superseded by:** none

## Context

A1 experiments demonstrated that local adaptation can change regime boundaries. Before adding A2 across domains, ownership boundaries must be stable so local adaptation, managerial control, and institutional change are not conflated.

## Decision

SOSE uses these agency boundaries:

- **A0:** fixed policy within fixed structure.
- **A1:** actors adapt local execution policy within fixed structure and authority.
- **A2:** management observes explicit metrics/signals, subject to configured delay/noise/cooldown, and selects allowed managerial actions with explicit transition costs.
- **A3:** objectives, institutional rules, or authority topology change; A3 remains outside SOSE 1.0.

A2 is implemented as a generic observation-decision-action loop. Each domain declares its allowed managerial action catalog.

## Decision drivers

- causal interpretability;
- cross-domain comparability;
- prevent hidden structural changes inside local policies;
- enable explicit adaptation/management costs.

## Consequences

### Positive

- A0/A1/A2 experiments remain interpretable;
- generic A2 runtime can coexist with domain-specific action sets.

### Negative / trade-offs

- domain authors must classify actions carefully;
- some realistic behaviors span boundaries and need explicit modeling choices.

### Constraints introduced

- A1 cannot change nominal organizational structure/authority;
- A2 cannot silently change institutional objectives;
- every agency action must be observable in agency-action/actor ledgers.

## Alternatives considered

### One generic adaptive-agent level

Rejected because local and managerial adaptation have different causal meaning.

### Domain-specific agency levels

Rejected because cross-domain experiments would lose comparability.

## Compatibility and migration

Existing A0/A1 semantics are retained. A2 is additive.

## Verification

AgencyConformance checks ownership, ledger accounting, deterministic CRN behavior, and prohibited boundary crossings.
