# PC6 distributed causal audit — PostgreSQL falsification protocol

Status: TDD increment; issue #406 remains open.

## PRD
An independent worker must never produce an audit of a half-committed domain effect. When a business certificate and Command deletion are committed atomically, every concurrent causal audit must return either the complete prior state or the complete next state, never an intermediate mixture. After worker restart, equivalent semantic history must yield the same deterministic digest regardless of physical claim order or lease epoch.

## TRD / fault matrix
1. Two real PostgreSQL connections use a distinct namespace. Worker A publishes and ACKs an inbound Logistics dispatch, stages a Command, commits shipment delivery and pauses **inside** the business certificate UnitOfWork before commit.
2. Worker B requests `audit_causal_history` during the pause. It must block behind the shared boundary transactional advisory lock and may only observe the new, complete `BusinessEffectApplied` record after the writer commits.
3. The audit must show exactly one applied effect, no pending effect, with immutable receipt retrievable from both connections. A later irrelevant claim/retry must not alter the semantic digest.
4. Close both connections and reconstruct from PostgreSQL. Require identical normalized audit report (including DAG edges, applied-effect count, digest) and immutable certificate content.
5. An independently inserted typed child of an unconsumed parent is validated as a DAG edge. Reclaiming an expired parent lease does not affect semantic truth. Physical claim timestamps/epochs are not semantic facts.

## ADR
Reuse existing `BusinessEffectService.boundary_transaction` and `audit_causal_history` coherent read snapshots; introduce **test evidence first** rather than new runtime abstractions before falsification shows them necessary. This protocol does not simulate a complete database crash, TCP partition or multiple overlapping PC6 orders; they remain separate required gates. Future experiments should also compare full operational states and preserve explicit settlement bindings across organizations and jobs.

## Promotion gate
Python 3.12–3.14, PostgreSQL, concurrency/chaos suite, authoritative coverage >=95%, and unchanged canonical historical v1 replay. No issue closure based solely on these two PostgreSQL tests.
