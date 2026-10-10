# PC6 typed ancestor certification gate (PRD / TRD / ADR)

## PRD
For **explicitly typed** boundary ancestry, a PC6 descendant may not be leased or consumed merely because its parent has a transport ACK and the accepted business Command is absent. For certified reference contracts, the authoritative BusinessEffectApplied receipt must exist and prove a compatible durable terminal entity. Untyped frozen organizational v1 message causes remain opaque/backward-compatible.

## TRD
* Parent must be CONSUMED with matching BoundaryConsumption.
* An outstanding parent Command blocks the descendant.
* When causation_kind=boundary and the parent contract has a certified terminal mapping, require its receipt by consumer_effect_id, matching effect/message/correlation identities; verify terminal entity state and non-regressing version.
* Perform the same predicate within claim and consume transaction. Missing proof blocks; conflicting proof/state fails closed.
* Do not alter historical v1 hashes or canonical protocols.

## ADR
Reuse existing authoritative receipt records and boundary_transaction locks. Do not add a second completion marker or widen the behavior for untyped v1 IDs; adoption is via explicitly typed successor messages. This gate is necessary, not sufficient, for end-to-end causal equivalence or arbitrary external exactly-once side effects.

## Falsification / promotion
The red-first regression stages a consumed parent and a typed child, removes a pending parent Command without issuing proof (previously admitted descendant), then asserts no child lease. It repairs parent truth, certifies, restarts SQLite and verifies the child becomes claimable. Also run PostgreSQL and full CI, >=95% coverage and historical replay before merge. Issue #406 remains open.
