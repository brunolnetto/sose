# Maintenance Repair and Overhaul organizational experiment v1 — prospective preregistration

**Status: PROPOSED — NOT FROZEN.** The executable design in `sose.organizational.mro_protocol` is subject to CI, review and a versioned hash manifest before any official run. Preflight outcomes must never be recast as independent official evidence.

## Research question and falsification

Can the existing PC5 Maintenance Repair and Overhaul reference reproduce its declared finite synthetic mechanisms and typed durable invariants using the same generic Gate-A protocol as Manufacturing, without changing generic orchestration, CRN pairing or analysis?

The protocol fails if an eligible invariant fails, an eligible intervention mechanism cannot be distinguished from paired baseline by its expected direction, or the runtime no longer reconstructs durable state accurately. Ineligible comparisons do not become eligible after observing favorable results.

## Design

- Numeric exogenous DOE coordinate: `quantity`, [1,20] spare parts.
- Arms: baseline, spare_part_shortage, emergency_preemption.
- Fixed A0 agency, no adaptivity, no A1/A2 generalization.
- Latin Hypercube sample of 6 exogenous settings, 3 arms, 2 deterministic replications = **18 worlds, 36 runs** expected.
- Design seed = root seed = 20261008; CRN enabled; warmup = 0; horizon = 24 logical hours.
- Outcomes and equivalence margins defined in executable `mro_protocol.py`.
- Statistical metadata: Holm, nominal 95% CI; repeated deterministic outcomes must not be interpreted as empirical uncertainty estimates.
- Primary falsification metric: `lead_time_seconds`, theta_fraction=0.5, used as synthetic protocol metadata only; not a claim of business improvement.

## Evidence and eligibility

- Eligible paired intervention effects: material_wait_count and lead_time_seconds for shortages; interruption_count and preemption_count for emergencies.
- Typed expected mechanisms/invariants: terminal closure, consumed quantity, empty remaining inventory, issue exactly once, wait and preemption counts.
- Non-claims: empirical calibration, external validity, stationary/queueing generalizations, managerial adaptation, stochastic precision and arbitrary production behavior.
- Domain-specific risk: strict logical clock advancement; bounded spare part capacity, double-issue and restart.
- Ineligible comparisons remain in the official dataset, never post-hoc discarded.

## Promotion gate

1. Pass standalone domain preflight and the common nine-check Gate-A matrix.
2. Verify protocol against PRD/TRD and process manifest; resolve all review findings.
3. Generate canonical `ExperimentProtocol` JSON and a manifest with immutable SHA-256 references to the exact adapter, protocol builder, PRD, TRD, canonical specification and run plan.
4. Merge the frozen scientific artifacts **before** the first official result execution.
5. Execute with frozen `uv.lock`, verify manifest prior to creating any world; publish full raw typed evidence, reports, checksum envelope, source SHA and lock digest to permanent GitHub Release.
6. Only after all of the above, interpret eligible claims and issue a result-specific report.

Freeze requires an independent gate for Maintenance Repair and Overhaul; no shared multi-domain freeze transaction. If code or methods change after freezing, issue a versioned deviation and re-freeze before rerunning.
