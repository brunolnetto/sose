# TRD-NNNN — <Title>

- **Status:** Draft
- **Owner:** <name/team>
- **Created:** YYYY-MM-DD
- **Last updated:** YYYY-MM-DD
- **Satisfies PRD(s):** <links>
- **Related ADRs:** <links or TBD>
- **Implementation PRs:** <links or TBD>

## 1. Technical context

Describe the current implementation, constraints, and why technical change is required.

## 2. Requirements mapping

| Requirement | Design response | Verification |
|---|---|---|
| PRD FR/QR | ... | test/gate/evidence |

## 3. Proposed design

Describe the design and component responsibilities.

Use Mermaid for architecture, state, sequence, or data-flow diagrams when useful.

## 4. Interfaces and contracts

Public APIs, internal protocols, schemas, configuration surfaces, event/command contracts, and compatibility expectations.

## 5. Data model and ownership

Define state, events, source-of-truth boundaries, identifiers, hashes, persistence, and lifecycle ownership.

## 6. Determinism and reproducibility

Where relevant, specify:

- identity generation;
- random streams/seeds/CRN;
- ordering semantics;
- replay guarantees;
- experiment hashes/provenance.

If not applicable, state why.

## 7. Failure, recovery, and concurrency semantics

Define:

- transaction boundaries;
- retries/idempotency;
- crash recovery;
- leases/fencing where relevant;
- partial-failure behavior;
- restart guarantees;
- conflict behavior.

## 8. Performance and scalability

Expected cardinalities, complexity, bottlenecks, resource limits, and performance gates.

## 9. Security and privacy

Trust boundaries, sensitive data, credentials, authorization, and data-retention implications. State `Not applicable` with rationale when appropriate.

## 10. Observability

Logs, metrics, traces, diagnostics, audit records, and user-visible failure information.

## 11. Testing and conformance

Required:

- unit tests;
- property/invariant tests;
- integration tests;
- restart/concurrency/chaos tests where relevant;
- cross-adapter/domain conformance where relevant;
- scientific verification/falsification gates where relevant.

## 12. Migration and compatibility

Schema/data migration, API compatibility, deprecation, rollout, rollback, and coexistence strategy.

## 13. Alternatives considered

Summarize viable alternatives. Consequential choices should link to dedicated ADRs rather than being decided only here.

## 14. Implementation plan

Break implementation into reviewable PRs with dependency order and exit criteria.

## 15. Risks and open questions

| Item | Impact | Mitigation / owner |
|---|---|---|

## 16. Acceptance / exit criteria

- [ ] All mapped PRD requirements satisfied.
- [ ] Required conformance gates green.
- [ ] Required ADRs accepted.
- [ ] Migration/recovery path validated.
- [ ] Documentation/evidence persisted.

## 17. Change history

| Date | Change | Rationale |
|---|---|---|
| YYYY-MM-DD | Initial draft | ... |
