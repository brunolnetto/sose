# PRD-0006 — Maintenance, Repair and Overhaul reference experiment v1
**Status:** Accepted — implementation scope approved; official preregistration and freeze remain separate gates.
**Parent:** PRD-0001, TRD-0001; ADR-0002/0003/0004/0005.

## User problem
Test whether the same domain-neutral framework works with resource-heavy processes, finite inventory, skills/capacity, precedence and emergency interruption — without special-casing the generic orchestrator.

## Requirements
- Reuse the PC5 MRO canonical and generic DomainReference/execution/conformance/report contracts.
- Candidate numeric exogenous axis: `quantity` (bounded by the built-in part capacity); secondary candidates `technician_capacity`, `maintenance_bay_capacity` and `spare_part_store_capacity` require demonstrated executable effects.
- Candidate arms: finite capacity contention, spare-part shortage and emergency preemption; only executable, durable and ethically irrelevant synthetic mechanisms may enter official v1.
- Preserve exactly-once part consumption, resource release/reacquisition, cancellation, recovery and identity/ownership semantics.
- Start A0-only, with explicitly disclaimed stochastic confidence precision when deterministic replications are identical.
- Freeze a domain-specific DOE/metrics/eligibility/provenance document before inspecting official outcomes.

## Falsification
Fail if model control requires runtime-state cheating, changes framework-level scheduler/CRN, bypasses the same cross-domain conformance suite, breaks resource ownership after rebuild, or shows mismatched raw evidence/reprojection.

## Exit gates
PC5 audited; semantic intervention preflight; generic conformance and restart; accepted TRD; frozen preregistration; official dataset/report with typed ground truth, eligible/ineligible comparisons and durable audit evidence.