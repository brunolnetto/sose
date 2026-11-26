# ADR-0012 — Seeded, additive workload generation for domain references

- **Status:** Proposed
- **Date:** 2026-11-26
- **Decision owners:** SOSE maintainers
- **Related PRD(s):** none
- **Related TRD(s):** none
- **Supersedes:** none
- **Superseded by:** none

## Context

Every builtin domain reference executes exactly one canonical case per run.

- `seed()` creates one reference case. `reconcile_tick()` advances that case and
  schedules its next semantic boundary. No domain creates new cases over time;
  `energy_utilities` recurring meter readings are a partial exception.
- Happy and sad paths are chosen by configuration, not sampled. The O2C reference
  fixes `demand={"sales_orders": 1}` and expresses deviations as flags
  (`partial_fulfillment`, `overdue_collection`). `recurring-domain-jobs.md`
  requires reconcilers to be "explicit about configured happy/sad-path choices".
- StateCharts declare transition weights through `@probabilistic_transitions`
  (MRO declares `cancel` 0.02 and `wait_for_material` 0.20, for example), and the
  engine exposes `StateChartFactory.decide()`. No domain, composition or
  organizational module calls `decide()`, so the stochastic layer designed in
  v0.3 (`docs/probabilistic-transition-graph.md`) is not exercised by the domains.
- The only arrival generator is the synthetic queue reference
  (`organizational/synthetic_runner.py`: Poisson arrivals, M/G/1 analytical
  reference). It is separate from the 28 catalog domains.

This is deliberate for the existing references. One deterministic case per
configured path gives exact INVARIANT and MECHANISTIC ground truth (ADR-0003). For
example, the O2C reference expects `collected = 1.0`, and its expected
`overdue_count` is the configured flag. The v1 O2C, MRO and manufacturing references
are frozen scientific evidence (ADR-0007).

The consequence is that domain runs carry no volume and no variation. Rate metrics
over a population (escalation rate, reopen rate, on-time rate, defect rate, fill
rate) are 0 or 1 by construction. Downstream consumers of the canonical experiment
data product (ADR-0005), such as dbt metric catalogs, cannot derive meaningful
metrics from them. A survey of an external 210-metric business catalog found about
40 metrics with structural support in SOSE domains, and almost none with
statistically meaningful values today.

No ADR forbids volume. The engine, scheduler, durable resources and persistence
already run many owners and cases (PC6, OD-5a). What is missing is a domain-level
workload primitive and the wiring of transition sampling.

## Decision

Introduce a **workload mode** for domain references. It is additive to the
existing canonical reference mode.

1. **Two execution modes per domain.**
   - The *canonical reference mode* stays exactly as it is: one configured case,
     configured paths, and byte-identical stored data.
   - A *workload mode* is opted into explicitly through configuration. Without it,
     nothing changes.
2. **A workload specification.** Workload mode is driven by a validated, domain-
   neutral `WorkloadSpec`:
   - an arrival process: `poisson(rate)`, `deterministic(interval)` or an explicit
     schedule;
   - a horizon, given as a maximum count of cases or a logical end time;
   - optional per-case attribute distributions declared by the domain.

   Arrivals become durable `ScheduledWork`, and each one creates a new case
   through the domain's own case constructor. No hidden end-to-end loops: each
   trigger still advances one logical tick.
3. **Seeded randomness only.** Every random draw comes from a stream derived with
   `scoped_seed(root_seed, domain, stream, case_identity)`. Draws include
   inter-arrival times, attribute draws and transition choices. A run is
   reproducible from its configuration and root seed. Case identities are
   deterministic (`deterministic_id`), so restart and replay produce the same
   cases.
4. **Sampled transitions.** In workload mode, a reconciler at a decision point asks
   `decide()` for the next enabled event. It uses the chart's declared weights, or
   weights overridden by configuration within declared bounds. Configured flags
   remain available, as a degenerate weight of 0 or 1, so a canonical path can
   still be forced.
5. **Ground truth reclassified for workload runs** (ADR-0003 taxonomy):
   - per-case INVARIANT claims (conservation, precedence legality, ledger
     exclusivity) still hold for every case and for the aggregate;
   - MECHANISTIC claims become expectations derived from configured rates and
     weights. Examples are offered load (`arrival_rate × service_time /
     capacity`) and an expected branch frequency (a weight, normalized over the
     enabled events). Each expectation is compared within a declared statistical
     tolerance;
   - a metric whose expectation is not derivable is recorded as having no
     eligible ground truth. It is never claimed as truth.
6. **Frozen evidence untouched.** The v1 references and their canonical runs keep
   their exact bytes. Workload mode never changes a canonical run's persisted
   records. Changing a default is a breaking change under ADR-0011.

## Decision drivers

- Downstream analytics (ADR-0005) need populations and variation, not single
  cases.
- Exact per-case ground truth and frozen evidence must survive.
- Reproducibility and restart equivalence are core SOSE guarantees. Randomness must
  be seeded and durable.
- The stochastic layer and the `demand` slot already exist. Wiring them is cheaper
  and more coherent than a parallel generator.

## Consequences

### Positive

- Rate, duration and utilization metrics become meaningful per domain.
- Domains exercise their declared sad paths (escalation, rework, cancellation,
  material wait) in proportion to their weights.
- One mechanism serves every domain, instead of bespoke per-domain loops.

### Negative / trade-offs

- Workload runs have statistical, not exact, mechanistic ground truth. Comparisons
  need tolerances and sample-size rules.
- Each domain must expose a case constructor and decision points before it
  supports workload mode. This is incremental work per domain.
- Larger runs make the PostgreSQL adapter's per-transaction cost (O(state)) more
  visible.

### Constraints introduced

- Workload mode must be opt-in. The default configuration of every domain stays
  canonical.
- Every random draw goes through a seeded, scoped stream; process-global
  randomness is forbidden.
- Arrivals and sampled decisions must be durable (ScheduledWork, transition
  events), so that a restarted run is equivalent to a continuous one.
- Ground-truth claims for workload runs must state their kind, tolerance and
  eligibility.

## Alternatives considered

### Alternative A — Run the canonical reference many times with different seeds

It is viable today and needs no code. It was not selected because canonical runs
are deterministic: their paths come from configuration, so N runs produce N
identical cases. It also gives no arrivals, queueing or contention between cases.

### Alternative B — Post-hoc synthetic data generation outside SOSE

Downstream tooling could generate plausible rows. It was not selected because the
rows would not come from executed StateCharts, resources and scheduling. They would
carry no causal evidence and could violate the domain invariants SOSE exists to
guarantee.

### Alternative C — Make every domain stochastic by default

It was not selected because it would change frozen v1 evidence and the default
behaviour of every existing job, and it would break exact per-case ground truth.

## Compatibility and migration

- No change to persisted data, APIs or defaults for existing jobs and references.
- New configuration fields are optional and are rejected for domains that do not
  yet support workload mode.
- Historical replays, frozen digests and the canonical attested sweeps must stay
  green and byte-identical.

## Verification

- Byte identity: canonical runs of all 28 domains are unchanged. Frozen v1 replays
  stay green.
- Reproducibility: the same seed and configuration produce identical stored data.
  A run restarted at every trigger equals a continuous run.
- Statistical conformance: for each workload-enabled domain, observed arrival
  counts and branch frequencies fall within the declared tolerances of their
  mechanistic expectations across a seed sample.
- Invariants hold for every case in every workload run.

## References

- ADR-0003 (ground-truth taxonomy), ADR-0005 (canonical experiment data product),
  ADR-0007 (versioned scientific freezes), ADR-0011 (deprecation policy).
- `docs/probabilistic-transition-graph.md`, `docs/architecture/recurring-domain-jobs.md`.
- `src/sose/organizational/synthetic_runner.py` (existing Poisson arrival
  reference), `src/sose/organizational/o2c_reference.py` (single-case demand).
