# OD-5a non-confirmatory operational mechanism pilot

Tracker: [#461](https://github.com/brunolnetto/sose/issues/461).
Depends on the #471 arm compiler. This increment is **NOT** the independent execution freeze and never authorizes the 17,920 confirmatory simulations.

## Operational falsification

- The integer `execution_slots` parameter becomes actual concurrent SimPy resource capacity with deterministic, separately identified `execution:<slot>` instances.
- Each dependency edge becomes an independently queued, causal handoff competing for a **single** coordinator resource. Removing a declared edge reduces handoff workload. The fixed reference coordinator capacity of one is a *pilot assumption*, not an inferred fact about organizations.
- `coordination_latency` changes only coordination-resource holding duration, never stage service demand.
- Every item/station receives an exogenously specified, unchanged positive service demand. The pilot does not sample new random service times across treatments; frozen mechanism/entity counter-CRN generation is a subsequent gate.
- A treatment's declared `transition_time` prevents its stages from executing before the transition ends; it is not treated as free extra capacity.
- Traces preserve `cause`, `model_spec_hash`, `config_epoch=0`, resource identity, and event interval. The adapter rejects duplicate stage requests, duplicate edge signals, and overlapping physical resource allocations.

## Scientific limits and next gates

The pilot consumes **small hand-constructed workloads only**, using an ephemeral SimPy backend. It does not yet certify durability, worker-death replay, persistent reservations, exact item/actor ledgers, dynamic source-hash execution freezes, signed cost schedules, or statistical inference. A recovered run must be shown equivalent to a continuous run using SOSE's **authoritative persistence**, not by serializing SimPy state; connecting this adapter to that boundary is the next TDD increment.

Do **not** reuse the pilot output as a confirmatory OD-5a sample. Before running any registered workload, separately freeze the full data-generating model, graph-construction rules for the Sobol coordinates, deterministic arrival/service CRN counters, lifecycle-cost schedule, actor/resource topology, H, seeds, source + `uv.lock` hashes, and independent hash-verification schema. The immutable v1 freezes are out of scope.
