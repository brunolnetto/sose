# ADR-0001 — Adopt PRD, TRD, and ADR governance

- **Status:** Accepted
- **Date:** 2026-10-07
- **Decision owners:** SOSE maintainers
- **Related PRD(s):** none — repository governance bootstrap
- **Related TRD(s):** none — repository governance bootstrap
- **Supersedes:** none
- **Superseded by:** none

## Context

SOSE now spans runtime semantics, persistence, domain canonicals, distributed execution, synthetic organizational experiments, agency models, and scientific evidence.

Historically, substantial design intent has been recorded across architecture notes, roadmap documents, PR descriptions, specifications, and implementation discussions. These artifacts are useful but do not provide a consistent separation between:

1. product/scientific intent;
2. technical design;
3. consequential architectural decisions.

As the project moves toward a multi-domain, configurable 1.0 product, decisions must remain understandable across many PRs and over long periods of development.

## Decision

SOSE adopts the documentation policy in `docs/governance/documentation-policy.md`.

New substantial work will use:

- PRDs for product/scientific requirements and acceptance criteria;
- TRDs for technical design and implementation/conformance plans;
- ADRs for consequential architectural decisions.

For multi-PR initiatives, the planning artifacts are accepted before substantial implementation begins.

Small changes may explicitly declare that no PRD/TRD/ADR impact exists rather than creating unnecessary documents.

Existing architecture documentation is not retroactively migrated.

## Decision drivers

- preserve intent across long-running multi-PR initiatives;
- prevent implementation PRs from silently defining product requirements;
- keep architecture decisions independently discoverable;
- create traceability from requirement to design to decision to evidence;
- improve scientific reproducibility and falsifiability;
- avoid documentation theater for routine changes.

## Consequences

### Positive

- consistent planning and review vocabulary;
- explicit scope and non-goals;
- architectural decisions become durable records;
- technical designs can be evaluated before implementation;
- easier handoff and long-run roadmap execution;
- stronger linkage between experiments, evidence, and claims.

### Negative / trade-offs

- substantial initiatives require additional upfront writing;
- maintainers must classify whether a change requires each artifact;
- documents must be maintained as scope evolves.

### Constraints introduced

- accepted ADRs are immutable except for typo/link corrections;
- substantial multi-PR initiatives require accepted planning documents first;
- implementation PRs must link relevant artifacts or explicitly justify exemption.

## Alternatives considered

### Continue with free-form architecture documents

Rejected because requirements, technical design, and architectural decisions remain mixed and difficult to trace.

### Require one generic design document

Rejected because product intent, implementation design, and architectural history have different lifecycles and review needs.

### Require PRD/TRD/ADR for every PR

Rejected because this creates documentation theater for bug fixes, tests, and mechanical changes.

## Compatibility and migration

No existing document is invalidated.

Existing architecture/specification documents remain authoritative in their current scope and may be referenced by new PRDs, TRDs, and ADRs.

## Verification

Conformance is reviewed through the pull-request documentation section and by checking that substantial initiatives link the required artifacts.

## References

- `docs/governance/documentation-policy.md`
- `docs/templates/prd.md`
- `docs/templates/trd.md`
- `docs/templates/adr.md`

## Notes

This ADR bootstraps the governance mechanism itself and therefore does not require a preceding PRD/TRD.
