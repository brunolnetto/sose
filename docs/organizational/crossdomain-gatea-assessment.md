# Three-domain Gate-A comparison and refactoring decision

**Status:** Evidence-backed characterization / **not an official preregistration**.  
**References:** Manufacturing v1 official synthetic report; PRD-0005/TRD-0005 (O2C); PRD-0006/TRD-0006 (MRO).

## Comparison matrix

| Dimension | Manufacturing | Order-to-Cash | MRO |
| --- | --- | --- | --- |
| PC5 process ownership | production/WIP, raw material, machine | sales order, credit, receivable, collection | maintenance work order, parts, technician, bay |
| Exogenous DOE axis | quantity | due_delay_hours | quantity |
| Intervention mechanisms | downtime; yield degradation | partial fulfillment; overdue escalation | spare shortage; emergency preemption |
| A0 fixed policy | yes | yes | yes |
| Typed durable evidence | production/WIP/container/preemption | order, receivable, collection events | work order, parts/container/preemption |
| Generic world and CRN contract | same | same | same |
| Generic eligibility/analysis | same | same | same |
| Restart/replay evidence | PC5 canonical | PC5 canonical | PC5 canonical |
| Official scientific freeze | v1 frozen/published | **not frozen** | **not frozen** |

The executable matrix test enforces the same nine Gate-A checks, deterministic
result hashes and ground-truth assessments for each reference. It does not
replace domain-specific tests or imply empirical validity.

## Actual duplication observed

1. The three reference adapters each assemble a fixed A0-only
   `DomainExperimentPlan`, `ExperimentProtocol`, agency configuration,
   and identical statistical/replication defaults. This is *candidate*
   infrastructure duplication, but formal protocol contents and eligibility
   are domain-owned. Extract only the clearly identical setup **after**
   approved domain freeze specifications identify which defaults are
   legitimately shared.
2. All three wrap durable evidence in Pydantic objects and translate it
   into `ExperimentObservation`. This repetition is **intentional**:
   generic reflection-based projection would hide semantics, provenance
   and typed claims. Keep it local.
3. All three write small per-domain branches for intervention application,
   runtime execution, `ground_truth` and `comparison_eligibility`.
   These are domain **mechanics**, not accidental duplication; no
   enterprise supermodel and no kernel changes.
4. O2C and MRO both exposed *clock consistency* as a portability trap:
   driving the ephemeral backend without advancing SOSE's logical clock
   can counterfeit durable lead-time effects. A **shared test invariant**
   (timestamps/durable observations must follow the authoritative clock)
   should precede any shared clock-driving utility. Current domain adapters
   do not justify a new runtime abstraction.

## Architecture falsification summary

The adapters demonstrate that the same interfaces can express discrete
production, administrative collections and inventory/preemption without
domain-specific changes to DOE, CRN pairing, conformance, typed truth or
analysis. These are *preflight* results, not independent empirical validation.
If a later domain requires a core change, record a failing matrix test
and its counterexample first.

## Next experiments (not yet executed officially)

**O2C:** Write and approve a domain-specific preregistration which freezes
the exogenous `due_delay_hours` range, baseline, fixed A0 controller,
eligible decision/transition claims, terminal collection invariants,
CRN/replication plan, and audit manifest. Verify sampled due delay changes
persisted collection time after tick quantization. Then execute and report.

**MRO:** Write and approve a separate preregistration freezing part quantity,
spare shortage/preemption mechanisms, time horizon, part conservation,
exactly-once issue, release/reacquisition and restart checks, plus eligible
paired effects. Never infer stochastic precision from identical deterministic
replications.

**Cross-domain refactor gate:** No kernel promotion until both protocols
are frozen and executed, with an explicit measured duplication inventory.
Shared helpers must leave Manufacturing's frozen result hash unchanged.

**Cluster expansion:** Only after those gates select Trading Company PC6
followed by asset-intensive and service clusters, maintaining process
ownership and separate experimental eligibility for each organization.
