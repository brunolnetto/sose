# ADR-0005 — Framework owns the canonical experiment data product

- **Status:** Accepted
- **Date:** 2026-10-07
- **Decision owners:** SOSE maintainers
- **Related PRD(s):** `PRD-0001`
- **Related TRD(s):** `TRD-0001`
- **Supersedes:** none
- **Superseded by:** none

## Context

The multi-domain experiment platform must produce outputs that can be consumed consistently by SQL, dbt, process mining, BI, notebooks, and scientific analysis.

If every domain owns its complete output schema, downstream tooling becomes adapter-specific and cross-domain reports cannot rely on stable semantics. If the persistence backend owns the schema, storage technology begins to define scientific/product meaning. If only raw events are standardized, users still need bespoke transformations for common experiment concepts such as worlds, runs, regimes, agency actions, and provenance.

SOSE therefore needs a stable ownership boundary for experiment output semantics.

## Decision

The **domain-neutral experiment framework owns the canonical experiment data-product envelope**.

The initial logical surfaces are:

- experiments;
- worlds;
- runs;
- entities;
- events;
- item ledgers;
- actor ledgers;
- resources;
- interventions;
- agency actions;
- metrics;
- regimes;
- ground-truth claims;
- experiment provenance.

The framework defines the canonical meaning, identity, versioning, and relationships of these surfaces.

Domains may add typed domain-specific extensions, but they may not redefine canonical field semantics.

Persistence/output adapters materialize the logical data product into supported technologies; they do not own its semantic contract.

Operational persistence remains authoritative for simulation/job truth. Analytical sinks remain asynchronous and idempotent under the existing durable outbox/checkpoint boundary; a sink outage must not roll back committed experiment progress.

## Decision drivers

- stable downstream analytics;
- domain-neutral scientific reporting;
- process-mining readiness;
- backend independence;
- reproducible provenance;
- compatibility discipline approaching 1.0;
- avoidance of a lowest-common-denominator domain schema.

## Consequences

### Positive

- consumers can build reusable SQL/dbt/process-mining tooling;
- experiment/report code receives one stable common envelope;
- domain extensions preserve meaningful domain detail;
- backend choice cannot silently change result meaning;
- provenance and ground-truth eligibility become first-class data.

### Negative / trade-offs

- schema evolution becomes a compatibility concern;
- extension rules must be governed to prevent uncontrolled schema growth;
- some outputs will be represented twice: canonical projection plus richer domain-native evidence.

### Constraints introduced

- canonical fields require versioned semantics;
- domain adapters cannot rename/reinterpret canonical concepts;
- simulator-private runtime metadata is not automatically part of the public data product;
- analytical sinks cannot participate in the authoritative operational transaction;
- publish/retry identities must be deterministic and idempotent;
- domain-specific extensions must be namespaced/versioned and must not be required for generic conformance unless promoted through a future contract change.

## Alternatives considered

### Domain-owned complete output schemas

Rejected because common tooling and cross-domain reports would require bespoke adapters for every domain.

### Backend-owned schemas

Rejected because SQLite/PostgreSQL/Parquet/Iceberg/DuckLake are implementation/materialization choices, not semantic owners.

### Raw event log only

Rejected because worlds, runs, regimes, ground-truth claims, intervention/agency metadata, ledgers, and provenance are explicit product concepts and should not need to be reconstructed ad hoc.

### One flat universal table

Rejected because it destroys relational meaning and encourages sparse lowest-common-denominator schemas.

## Compatibility and migration

Existing evidence JSON remains valid historical evidence.

The new canonical product will be introduced additively and versioned. Existing operational/source interfaces are not redefined by this ADR.

Existing analytical-sink semantics remain authoritative: operational progress commits first with durable delivery intent; sinks advance independently and idempotently.

## Verification

`DataProductConformance` must verify across the pilot domains and supported reference backends that:

- canonical identities and foreign-key relationships are stable;
- equal semantic experiments produce equal canonical hashes independent of materialization backend;
- domains may add extensions without altering canonical semantics;
- provenance binds protocol, domain/model identity, seeds, run/world identities, ground-truth claims, and report artifacts;
- restart does not duplicate canonical rows/events;
- sink retry after publish-before-ack is idempotent;
- generic reports consume only canonical surfaces plus explicitly declared optional extensions.

## References

- `docs/product/requirements/prd-0001-multidomain-synthetic-experiment-platform.md`
- `docs/technical/requirements/trd-0001-domain-neutral-reference-experiment-framework.md`
- `docs/architecture/analytical-sinks.md`
- `docs/architecture/source-interface.md`
- `docs/architecture/process-mining.md`

## Notes

Substantive changes to canonical data-product ownership require a superseding ADR after acceptance.
