# TRD-0006 — MRO experiment adapter
**Status:** Draft.
**Implements:** PRD-0006; ADR-0002/0003/0004/0005.

## Ownership
MRO owns domain mechanics and adapter projections. Framework owns DOE, design index, world/arm identities, CRN pairing, orchestration, generic effects and publication. The OLTP/store remains authoritative; SimPy remains ephemeral.

## Semantic preflight
1. Inspect built-in `MROConfig` constraints and establish safe bounded numeric DOE axes.
2. Demonstrate that selected intervention arms alter durable mechanics rather than merely scenario labels.
3. Preserve warehouse spare-parts ownership and exactly-once consumption across interruption/restart.
4. Prove technician/bay capacity contention, finite delay, emergency priority and cancellation/recovery through typed evidence as appropriate to selected arms.
5. Verify deterministic seeds and CRN signatures, evidence hashes, observations, invariant/mechanistic ground truths and eligibility determined from configured inputs.

## Conformance tests
Repeat exactly the same Gate-A suite used by Manufacturing and O2C, plus MRO-specific resource conservation/ownership and rebuild tests. A 2-point/2-replication protocol is **preflight only**, never official evidence.

## Refactor rule
Record abstraction breaks and duplication in adapters, then refactor only shared accidental complexity confirmed by all three pilots. No early kernel promotion; promotion requires all three domain suites and process prerequisites.
