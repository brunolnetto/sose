# PC6 concurrent Cards Payments guard — PRD / TRD / ADR

## PRD
The multi-customer real-statechart PostgreSQL gate on main fails nondeterministically when two independent customers contend for the Cards authorization/settlement processor. Preserve exact statechart semantics, business correctness, immutable historical v1 artifacts and concurrency evidence. A transient shared processor busy result must not be misclassified as permanent payment authorization failure.

## TRD
The existing failing test executes two real customer flows sharing a PostgreSQL namespace with ingress synchronized at a barrier. Add a PostgreSQL advisory session guard scoped to `cards_payments.authorization_settlement` surrounding the entire authorized → captured → settlement_pending → settled multi-transaction statechart, analogous to the accepted R2R posting guard. The guard is optional for non-Postgres stores, preserving legacy default semantics. Add independent two-worker guard serialization regression in PostgreSQL CI. Maintain existing full domain integration test as hard gate; do not relax assertions or delete schedules.

## ADR
Session-scoped lock is required because engine operations span several UnitOfWork transactions, and ordinary transactional locks would release between transitions. Named guards currently serialize all customers for a given processor; this is correct for shared capacity but is not evidence of multi-customer parallelism within that particular processor. The full PC6 concurrency test still synchronizes ingress and allows overlap in other stages. Causal scheduler integrity and orphan scheduled Commands remain a separate failure mode and must be falsified rather than masked.

## Gate
Red-first main failure logs, exact-head Python 3.12–3.14, PostgreSQL integration, coverage >=95%, chaos, benchmarks and frozen historical replay. No premature merge. Keep #406 OPEN.
