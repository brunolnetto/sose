# Manufacturing Organizational Experiment v1 — Preregistration

- **Status:** Draft
- **Domain:** Manufacturing
- **Related PRD:** PRD-0004
- **Related TRD:** TRD-0004
- **Canonical machine-readable protocol:** `manufacturing-experiment-v1.json`

## Scientific question

Can the SOSE Manufacturing PC5 canonical preserve preregistered durable invariants and recover the mechanistic effects of implemented finite disruption scenarios under deterministic replicated A0 execution?

## Claims

1. **INVARIANT:** applicable MFG-01 through MFG-10 invariants hold in every valid official world.
2. **MECHANISTIC — yield:** a configured yield-degradation arm reduces durable output/yield relative to its paired nominal world when terminal comparison is eligible.
3. **MECHANISTIC — downtime:** an eligible machine-downtime arm produces durable breakdown/machine-down evidence and recovery requires machine reacquisition.

## Explicit non-claims

- no empirical validation;
- no A1/A2 agency claim;
- no queueing/stationary analytical claim;
- no congestion/load-regime claim;
- no demand-surge effect claim unless preflight proves a durable executable workload effect before freeze.

## Candidate axes and arms

The draft allows only executable Manufacturing inputs:

- `quantity` — bounded during preflight;
- `quality_outcome` — categorical configured condition;
- nominal arm;
- machine downtime;
- yield degradation.

Demand surge remains excluded until a preflight proves that the current single-order reference converts that scenario into durable additional workload/pressure.

## Freeze gate

This preregistration is **not yet frozen**.

Before official execution:

1. quantity bounds must be fixed;
2. candidate arms must pass semantic preflight;
3. DOE/sample size must be fixed;
4. replication count/root seed must be fixed;
5. all eligibility/falsification rules must be finalized;
6. the JSON protocol must be canonicalized and hash-addressed;
7. status changes to `frozen`.

No official result may be executed/inspected before that freeze.
