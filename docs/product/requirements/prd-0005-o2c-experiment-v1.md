# PRD-0005 — Order-to-Cash reference experiment v1
**Status:** Accepted — implementation scope approved; official preregistration and freeze remain separate gates.
**Parent:** PRD-0001, TRD-0001; ADR-0002/0003/0004/0005.

## User problem
Test whether a framework proved with finite Manufacturing mechanics generalizes to a decision- and handoff-oriented administrative workflow without adding O2C-specific orchestration.

## Requirements
- Reuse the generic DomainReference descriptor, framework-owned DOE/worlds/CRN, typed ground truth, conformance and report contracts unchanged.
- Reuse the audited O2C process canonical; preserve its credit, fulfillment, receivable and collection ownership boundaries.
- Preflight only executable mechanisms already proven by O2C integration/restart tests: credit hold, partial fulfillment, overdue collection, finite fulfillment contention.
- Select a **numeric exogenous axis** only after confirming it changes the executable mechanism; candidate: configured receivable `due_delay_hours ∈ [1,5]` (finite scheduled transition), not the order amount. Never use realized throughput, WIP or delay as DOE inputs.
- Start with A0; no fictitious A1/A2 controller. No empirical/stationary claims.
- Separate eligible mechanistic/invariant claims from ineligible comparisons; never tune the official experiment after observing outcomes.
- A frozen domain preregistration shall specify arms, sample size, replication policy, metrics, falsification criteria, eligibility and provenance before official execution.

## Falsification
Fail if the adapter manipulates world sampling/CRN, requires O2C conditional logic in generic orchestration, extracts private engine state, produces unmatched raw evidence hashes, misclassifies effects from endogenous outputs, or violates any existing O2C restart/replay invariant.

## Exit gates
(1) PC5 audited; (2) domain-specific TRD accepted; (3) semantic arm preflights and generic conformance green; (4) frozen preregistration; (5) audited evidence and report, with no empirical inference.
**Design finding (preflight review):** The built-in `amount` parameter is exogenous but does not modulate process timing, routing, or decision policy in the current one-order runtime. It is therefore **excluded** from the DOE. The executable `due_delay_hours` setting is the numeric axis; the preflight must demonstrate a durable lead-time change across design points before freezing an official protocol.
