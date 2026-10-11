# OD-5a — Pre-execution manipulation and equal-budget admission

**Tracker:** [#461](https://github.com/brunolnetto/sose/issues/461).  
**Scientific design:** [OD-5a prospective v2](od5a-structure-capacity-preregistration-v2.md).  
**Status:** arm *compilation and rejection contract only*; it neither simulates business events nor freezes/produces confirmatory results.

## Scope

The first executable increment creates five immutable, hash-addressed `ModelSpec` worlds using the existing `ModelIntervention` boundary. This is a prerequisite for the independently frozen experiment, not its execution or its certification.

Every baseline must predeclare a directed acyclic `routing.dependency_edges` graph and numeric configuration parameters `execution_slots` (integer parallel physical slots), `coordination_latency`, and `arrival_rate`, each with `parameter_evidence`. The baseline has stable model identity and no treatment cost.

| Arm | Authorized mutations | Prohibited mutations |
|---|---|---|
| `baseline` | none | all treatment changes |
| `capacity_only` | increase integer `parameters.execution_slots` | handoffs, coordination, arrivals, calendars, service-distribution parameters |
| `topology_only` | remove a declared nonempty proper subset of existing dependency edges | capacity, coordination latency, arrivals, other routing |
| `coordination_only` | decrease `parameters.coordination_latency` | capacity, handoff edges, service distribution |
| `structural_combined` | topology removal and coordination decrease, together | capacity and every other configured field |

The compiler rejects duplicate/cyclic dependency graphs, phantom edge removal, zero-effect interventions, unpriced transition time, missing cost arms and any movement in declared cost beyond the preregistered 1% tolerance.

## Explicit economic charge

The **total charged lifecycle budget per arm** is

`B = operating_cost_at_horizon + transition_cost + transition_time * shadow_price_per_transition_time`.

This is a *declared deterministic* cost ledger supplied **before** simulation. The `ModelIntervention.operating_cost` field receives the **total at H**, not an unannounced time rate. Rates and the choice of horizon H must still be preregistered in the future execution freeze. A positive transition duration requires an explicitly positive shadow price. Costs are compared against the capacity-only treatment; baseline is **not** a matched-cost arm. All treated budgets must be positive and differ by at most 1% of the declared capacity-only budget.

Every output `OD5aCompiledArm` exposes immutable model and intervention identities and an additional admission hash that includes charged budget components (so identical total spend with differently distributed cost is not silently equivalent).

## Why this is not OD-5a execution

This module proves **syntactic manipulation and pre-treatment admission only**. A changed `ModelSpec.routing.dependency_edges` is not yet proof that an operational process changes handoffs as intended. Before confirmatory runs, #461 still requires:

1. A process adapter whose generated events demonstrate that topology/coordination manipulations actually change those mechanisms, while independent arrival and service streams remain unchanged.
2. A fixed observation horizon H and numeric exogenous bounds, a complete signed-off cost schedule, root seed, source/lock/model/protocol/intervention hashes and a sealed design matrix.
3. Worker-restart equivalence of event/ledger/cost and resource truth, including causation IDs and `config_epoch`.
4. A *second* immutable execution-freeze gate, followed by all 17,920 registered arm-world-replication executions and independent canonical hash verification.
5. Complete publication of null, harmful and nonidentifiable regimes with the 21-comparison inference rule and explicitly bracketed or nonexistent `R*.`

Do not run worlds on this increment; do not mutate v1 Manufacturing/O2C/MRO or PR-review results. This is a finite pre-execution step, never an authorization to claim the organization thesis is supported.
