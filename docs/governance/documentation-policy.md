# PRD / TRD / ADR Documentation Policy

## Purpose

SOSE uses three complementary decision artifacts so that product intent, technical design, and architectural decisions remain explicit, reviewable, and durable.

The documents answer different questions:

- **PRD — Product Requirements Document:** why the capability exists and what outcomes/requirements it must satisfy.
- **TRD — Technical Requirements Document:** how the capability will be designed, implemented, tested, operated, and evolved.
- **ADR — Architecture Decision Record:** which consequential architectural choice was made, why it was chosen, and what alternatives/consequences were accepted.

Implementation PRs must not become the first place where a material product or architectural decision is discovered.

## Canonical locations

```text
docs/product/requirements/
    prd-NNNN-<slug>.md

docs/technical/requirements/
    trd-NNNN-<slug>.md

docs/architecture/decisions/
    adr-NNNN-<slug>.md
```

Templates live under `docs/templates/`.

IDs are monotonically increasing within each artifact type. IDs are never reused.

## Artifact responsibilities

### PRD

A PRD owns:

- problem statement and user/business/scientific motivation;
- target users and workflows;
- goals and explicit non-goals;
- functional and quality requirements;
- measurable success criteria;
- constraints and acceptance criteria;
- rollout expectations and product-level risks.

A PRD must not prescribe implementation details unless they are themselves requirements.

### TRD

A TRD owns:

- technical context and current state;
- system boundaries and component responsibilities;
- interfaces, schemas, data ownership, and persistence;
- determinism/reproducibility semantics where relevant;
- failure, retry, recovery, concurrency, and migration semantics;
- performance/scalability expectations;
- observability and operational behavior;
- test strategy and conformance gates;
- implementation plan and rollout.

A TRD must link to the PRD(s) it satisfies when a PRD applies. When the change matrix permits a PRD-less TRD, the TRD must explicitly record `Satisfies PRD(s): none` with a short rationale.

### ADR

An ADR owns one consequential architectural decision.

Typical ADR subjects include:

- source-of-truth or ownership boundaries;
- runtime/persistence semantics;
- public API compatibility decisions;
- backend/dependency selection with long-lived consequences;
- data-model or protocol choices;
- distributed coordination/concurrency models;
- architectural invariants;
- decisions that are expensive or risky to reverse.

An accepted ADR is immutable in its recorded context, decision, drivers, consequences, and alternatives. Typo/link corrections plus lifecycle metadata updates (`Status`, `Superseded by`, and reciprocal supersession links) are allowed. A changed decision requires a new ADR that supersedes the old one.

## When each artifact is required

| Change | PRD | TRD | ADR |
|---|---:|---:|---:|
| New user-facing capability or workflow | required | usually required | if architectural choice |
| New domain/reference organization | required | required | if new architectural pattern |
| New experiment family or agency level | required | required | if semantics/invariants change |
| New public API or compatibility contract | required | required | required for consequential contract choice |
| Persistence/runtime/concurrency change | if product behavior changes | required | normally required |
| New backend or authoritative store | if user-visible | required | required |
| Cross-module architectural refactor | not always | required | required if boundaries/invariants change |
| Local bug fix preserving contracts | no | no | no |
| Tests/coverage only | no | no | no |
| Documentation clarification only | no | no | no |
| Mechanical dependency patch | no | no | only if architecture/compatibility changes |

The requirement is based on **decision scope**, not line count.

## Exemption rule

A PR that does not require PRD/TRD/ADR must state:

```text
Documentation impact: none
Reason: <why no product, technical-contract, or architectural decision changes>
```

This prevents both documentation theater and silent architectural drift.

## Required sequence

For initiatives expected to span multiple implementation PRs:

```text
PRD
 ↓
TRD
 ↓
ADR(s), when needed
 ↓
implementation PRs
 ↓
validation / evidence / release
```

When required by the change matrix, When required by the change matrix, PRD and TRD should be accepted before substantial implementation begins. A TRD may intentionally have no PRD when the matrix permits it, but that exemption must be explicit in the TRD. A TRD may intentionally have no PRD when the matrix permits it, but that exemption must be explicit in the TRD.

For a tightly scoped single-PR change, a PRD/TRD may accompany the implementation only when the change is not cross-cutting and reviewers can evaluate the design independently of the code. Cross-cutting decisions require the planning artifacts first.

## Lifecycle

### PRD / TRD

Allowed statuses:

- `Draft`
- `Accepted`
- `Implemented`
- `Superseded`
- `Rejected`

Material scope changes after acceptance must be visible in the document change history. If the original contract is no longer recognizable, create a new document and mark the old one superseded.

### ADR

Allowed statuses:

- `Proposed`
- `Accepted`
- `Deprecated`
- `Superseded`
- `Rejected`

Accepted ADRs are append-only historical records with respect to decision content. Their lifecycle status and supersession metadata may be updated to reflect deprecation or replacement. Architectural evolution itself is represented by new ADRs.

## Traceability

Every artifact must link outward and implementation PRs must link back.

Expected chain:

```text
PRD-xxxx
  └─ TRD-yyyy
       ├─ ADR-zzzz
       └─ implementation PR(s)
             └─ tests / evidence / release
```

A TRD may satisfy multiple PRDs, and a PRD may require multiple TRDs.

## Definition of Ready

A substantial implementation is ready to start when:

1. the problem and non-goals are explicit;
2. acceptance criteria are testable;
3. technical ownership/boundaries are explicit;
4. persistence/failure semantics are defined where relevant;
5. consequential architecture decisions are recorded;
6. unresolved questions that could invalidate implementation are closed or explicitly deferred.

## Definition of Done

A substantial capability is done when:

1. implementation satisfies the linked PRD acceptance criteria;
2. TRD implementation and conformance gates are green;
3. ADR consequences/constraints are respected;
4. tests and evidence are linked;
5. operational/recovery behavior is documented when relevant;
6. public documentation and release notes are updated when required.

## Scientific and synthetic experiments

SOSE experiments require additional discipline.

A PRD must state the scientific/product question and claims that the experiment may or may not support.

A TRD must define:

- exogenous inputs versus endogenous outputs;
- generating mechanics;
- ground-truth class;
- DOE and replication policy;
- seed/CRN semantics;
- eligibility/exclusion rules;
- metrics and falsification criteria;
- provenance and artifact persistence.

An ADR is required when changing foundational experiment semantics such as randomness, ground-truth classification, agency ownership, evidence eligibility, or reproducibility guarantees.

## Existing documentation

Existing architecture documents remain valid and are not retroactively rewritten into ADRs/TRDs.

From adoption of this policy onward:

- new material product requirements use PRDs;
- new substantial technical designs use TRDs;
- new consequential architectural decisions use ADRs.

Historical architecture documents may be referenced as context by new artifacts.

## Review standard

Reviewers should challenge:

- requirements hidden in TRDs;
- implementation choices hidden in PRDs;
- multiple unrelated decisions packed into one ADR;
- code that changes a documented contract without updating its artifact;
- retrospective documents written only after implementation;
- ADRs used for routine implementation details.

The goal is decision quality and traceability, not document volume.
