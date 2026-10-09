# TRD-0005 — O2C experiment adapter
**Status:** Draft.
**Implements:** PRD-0005; inherits ADR-0002/0003/0004/0005.

## Adapter boundary
Place O2C adapter in `sose.organizational`. Implement the same DomainReference methods used by Manufacturing: descriptor, parameter_space, build_model, interventions, execute, observation, ground_truth, classify_regime, comparison_eligibility and CRN signature.

The adapter may wrap the existing O2C simulation/reconciliation; it must not implement its own DOE, comparison pairing, world hashing or report.

## Required preflight
- Confirm `amount` binding is exogenous and durable.
- Verify at least one policy/decision mechanism (credit hold or collection outcome) and one handoff mechanism (fulfillment/collection) produces distinct *durable* arm evidence; otherwise exclude that arm.
- Verify terminal and sad paths, including restart and exactly-once effects.
- Output typed numerical observations plus original evidence. Reject claims not exposed by observations.
- A0 fixed only until local agency policy implementation exists.
- Use 2-point/2-replication preflight separate from a later official preregistration; do not publish preflight as scientific evidence.

## Tests
TDD: protocol validation, expected cardinality, CRN sharing, stable identities, deterministic replay, no hidden-state observations, typed truth integrity, eligibility defined from configuration, conformance report and restart equivalence.
Do not expand generic framework interfaces until a demonstrated conflict is documented; record each actual duplication for later transversal refactor.