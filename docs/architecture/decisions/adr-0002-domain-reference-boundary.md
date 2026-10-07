# ADR-0002 — DomainReference is the extension boundary for organizational domains

- **Status:** Proposed
- **Date:** 2026-10-07
- **Decision owners:** SOSE maintainers
- **Related PRD(s):** PRD-0001
- **Related TRD(s):** TRD-0001
- **Supersedes:** none
- **Superseded by:** none

## Context

SOSE has many executable domain examples, while the new synthetic experiment stack currently knows too much about one reference process. A stable extension boundary is needed before porting more organizations.

## Decision

Introduce one domain-facing `DomainReference` contract. Core experiment orchestration depends only on this contract and common SOSE types.

Domains own baseline ModelSpecs, exogenous parameter declarations, intervention catalogs, domain run mechanics, ground truth, and regime classification.

Core code must not branch on domain identifiers.

## Decision drivers

- avoid duplicated experiment orchestration;
- preserve domain-specific semantics;
- make cross-domain conformance possible;
- keep engine/framework independent from the domain catalog.

## Consequences

### Positive

- new domains become adapters/plugins;
- shared DOE/CRN/provenance logic has one implementation;
- domain capability gaps become explicit.

### Negative / trade-offs

- contract design must avoid becoming a lowest-common-denominator interface;
- some domains need typed extensions.

### Constraints introduced

- domain-specific behavior cannot be added to core via domain-name conditionals;
- canonical result meanings cannot be overridden by a domain.

## Alternatives considered

### Inheritance hierarchy per industry

Rejected because industry taxonomies do not align cleanly with shared process mechanics.

### Independent experiment package per domain

Rejected because determinism, CRN, persistence, and report behavior would drift.

## Compatibility and migration

Current reference experiment functions become compatibility adapters until their behavior is represented through `DomainReference`.

## Verification

Manufacturing, O2C, and MRO must pass one common `DomainReferenceConformance` suite.
