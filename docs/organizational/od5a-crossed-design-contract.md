# OD-5a crossed Sobol design — pre-execution run identity contract

**Parent:** [#461](https://github.com/brunolnetto/sose/issues/461).  
**Research registration:** [OD-5a v2](od5a-structure-capacity-preregistration-v2.md).  
**State:** deterministic **design materialization**, *not* a stochastic simulation and not an execution freeze.

## Why the design is crossed

The scientific break-even `R*` is defined over configured nominal offered-load `rho_nominal` at **fixed other coordinates**. A standard six/seven-dimensional Sobol cloud with one unique load value at each nuisance stratum could confound structural heterogeneity with load and cannot identify adjacent-load crossing brackets.

The registered design therefore uses **32 Sobol strata over exactly six non-load exogenous coordinates**, crossed with seven fixed nominal-load settings `[0.4,0.6,0.8,1.0,1.2,1.4,1.6]`, yielding 224 exogenous configurations, five fixed arms and 16 replicated run identities per arm: **17,920 total run identities**.

## Contracts

- `build_od5a_configured_worlds(bounds, design_seed)` calls the existing framework Sobol sampler and counter-RNG digital shift; bounds and seed are injected by a *later immutable freeze*, not guessed from outcomes.
- Bound keys must equal `calendar_availability`, `dependency_density`, `routing_complexity`, `nominal_service_distribution`, `nominal_coordination_latency`, `declared_intervention_cost_ratio`. Realized WIP, utilization and outcome-derived fraction coordinates are rejected.
- Within each stratum, the same sampled non-load values appear at **every** load level. The denominator of offered load remains **baseline nominal execution capacity**. There is no post-treatment sorting, reclassification or regressing world coordinate.
- `iter_od5a_run_keys` rejects missing/duplicate/out-of-order worlds, mixed Sobol seeds, corrupted loads or injected endogenous dimensions. Each fixed exogenous world receives one stable `crn_group` shared across all five arms, and 16 different immutable replication identities.
- The CRN group is an eligibility *intention*: when a structural treatment changes item/opportunity identity, the eventual runtime must report a nonpairable mechanism; identical group names do not themselves establish valid counter-CRN pairing.

## Unresolved runtime meaning

One preregistered coordinate, `nominal_service_distribution`, cannot be interpreted as an arbitrary unconstrained float by a runtime. The **execution freeze must specify its physical meaning** (for example, shape/CV under a fixed registered service-time family), fixed units, acceptable numeric bounds and how it affects service-time draws, before testing any outcomes. The same applies to converting `dependency_density` and `routing_complexity` into an exact DAG of stage dependencies: both are inputs to a frozen world constructor, not observed categories chosen post-hoc.

This increment intentionally does **not** set parameter bounds, final cost schedule, root seed, horizon, source/lock hashes, resource/actor instance policy, SimPy event adapter or confirmatory outcomes. Those requirements remain in #461. Existing Manufacturing/O2C/MRO v1 frozen evidence is unaffected.
