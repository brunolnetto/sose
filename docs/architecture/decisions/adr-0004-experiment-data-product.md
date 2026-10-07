# ADR-0004 — Canonical experiment data product is owned by the framework

- **Status:** Proposed
- **Date:** 2026-10-07
- **Decision owners:** SOSE maintainers
- **Related PRD(s):** PRD-0001
- **Related TRD(s):** TRD-0001
- **Supersedes:** none
- **Superseded by:** none

## Context

Experiment outputs currently vary by study. Multi-domain use requires stable analytical surfaces without leaking simulator-private runtime state.

## Decision

The experiment framework owns a canonical data-product envelope covering experiments, worlds, runs, events, ledgers, resources, interventions, agency actions, metrics, regimes, ground truth, and provenance.

Domains may add extension data but cannot redefine canonical semantics.

Persistence adapters materialize this logical product; storage technology does not own the schema semantics.

## Decision drivers

- stable downstream analytics;
- process-mining/data-engineering interoperability;
- cross-domain reports;
- backend independence.

## Consequences

### Positive

- one contract for SQL/Parquet/Iceberg/DuckLake consumers;
- easier dbt/BI/process-mining integration;
- provenance remains first-class.

### Negative / trade-offs

- schema evolution requires compatibility discipline;
- extension strategy must avoid schema explosion.

### Constraints introduced

- simulator-private metadata stays outside public source/product tables unless explicitly classified;
- canonical fields require versioned semantics.

## Alternatives considered

### Domain-owned output schemas only

Rejected because cross-domain tooling would require bespoke adapters.

### Persist raw event logs only

Rejected because users need canonical analytical views and ledgers.

## Compatibility and migration

Current evidence JSON remains valid historical evidence; new runs gain canonical data-product materializations.

## Verification

DataProductConformance runs across all pilot domains and supported reference backends.
