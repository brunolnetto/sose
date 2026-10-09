# Order-to-Cash organizational experiment v1 — prospective preregistration

**Status: PROPOSED — NOT FROZEN.** The executable design in `sose.organizational.o2c_protocol` is subject to CI, review and a versioned hash manifest before any official run. Preflight outcomes must never be recast as independent official evidence.

## Research question and falsification

Can the existing PC5 Order-to-Cash reference reproduce its declared finite synthetic mechanisms and typed durable invariants using the same generic Gate-A protocol as Manufacturing, without changing generic orchestration, CRN pairing or analysis?

The protocol fails if an eligible invariant fails, an eligible intervention mechanism cannot be distinguished from paired baseline by its expected direction, or the runtime no longer reconstructs durable state accurately. Ineligible comparisons do not become eligible after observing favorable results.

## Design

- Numeric exogenous DOE coordinate: `due_delay_hours`, [1,5] hours.
- Arms: baseline, partial_fulfillment, overdue_collection.
- Fixed A0 agency, no adaptivity, no A1/A2 generalization.
- Latin Hypercube sample of **4 distinct one-hour quantized timing strata**, 3 arms, 2 deterministic replications = **12 worlds, 24 runs** expected. The continuous sampling range [1,5] is resolved into four durable timing outcomes (2–5 hours after ceiling to logical ticks). A six-point DOE would contain duplicate executable timing configurations and is prohibited in v1.
- Design seed = root seed = 20261008; CRN enabled; warmup = 0; horizon = 24 logical hours.
- Outcomes and equivalence margins defined in executable `o2c_protocol.py`.
- Statistical metadata: Holm, nominal 95% CI; repeated deterministic outcomes must not be interpreted as empirical uncertainty estimates.
- Primary falsification metric: `order_to_cash_seconds`, theta_fraction=0.5, used as synthetic protocol metadata only; not a claim of business improvement.

## Evidence and eligibility

- Eligible paired intervention effects: partial_fulfillment_count for partial fulfillment; overdue_count and order_to_cash_seconds for overdue collection.
- Typed expected mechanisms/invariants: collected, monetary amount conservation, partial count, overdue and escalation count.
- Non-claims: empirical calibration, external validity, stationary/queueing generalizations, managerial adaptation, stochastic precision and arbitrary production behavior.
- Domain-specific risk: tick-rounded due-time precision; deterministic replications.
- Ineligible comparisons remain in the official dataset, never post-hoc discarded.

## Promotion gate

1. Pass standalone domain preflight and the common nine-check Gate-A matrix.
2. Verify protocol against PRD/TRD and process manifest; resolve all review findings.
3. Generate canonical `ExperimentProtocol` JSON and a manifest with immutable SHA-256 references to the exact adapter, protocol builder, PRD, TRD, canonical specification and run plan.
4. Merge the frozen scientific artifacts **before** the first official result execution.
5. Execute with frozen `uv.lock`, verify manifest prior to creating any world; publish full raw typed evidence, reports, checksum envelope, source SHA and lock digest to permanent GitHub Release.
6. Only after all of the above, interpret eligible claims and issue a result-specific report.

Freeze requires an independent gate for Order-to-Cash; no shared multi-domain freeze transaction. If code or methods change after freezing, issue a versioned deviation and re-freeze before rerunning.
