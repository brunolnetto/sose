# Synthetic organizational reference experiment v1

This is a **verification experiment against known synthetic generating mechanics**.
It is not empirical validation and uses no observed organizational outcomes.

## Fixed design

- 12 Latin-hypercube design points.
- Three arms per design point: baseline, capacity expansion, and strictly rework-reducing automation.
- 36 worlds total, 16 CRN replications each, 576 replicated runs.
- Fixed design and root seed: `20261007`.
- Regime coordinates use exogenous inputs only: arrival rate, service capacity, service CV, rework probability, and transition cost.
- Baseline rework probability is sampled in `[0.05, 0.40]`; automation sets it to `0.02`, so the intervention reduces rework in every world.
- WIP, throughput, queueing delay, and lead time are outputs only.

## Result

- Stable worlds: **20 / 36**.
- Saturated worlds: **16 / 36**.
- Mean relative error versus stationary M/G/1 ground truth in stable worlds: **6.35%**.
- Direction recovery for eligible non-null intervention effects: **8 / 8 = 100%**.
- Null-region effects: **0**.
- Regime transitions across intervention arms:
  - stable → stable: **8**
  - saturated → stable: **8**
  - saturated → saturated: **8**
  - stable → saturated: **0**

## Interpretation

The reference generator recovers intervention direction for every eligible effect in this design. Analytical lead-time discrepancy remains concentrated near the critical boundary. For stable worlds with offered load below 0.70, mean relative error is approximately **2.36%**; for the two stable worlds with offered load at or above 0.95, mean relative error is approximately **33.11%**. This is consistent with slow finite-horizon convergence of queue backlogs as `ρ → 1`.

The result therefore supports the intended verification claim: **SOSE can synthesize organizational process data from explicit exogenous mechanics and recover known regime/intervention structure without feeding endogenous outputs back into the DOE coordinates.**

This does **not** establish empirical validity for real organizations, A1/A2 agency behavior, or transient intervention dynamics. Those are separate experiments.

## Provenance

- Protocol hash: `32479ce47f24d5fbb3f9098cd796f4e62205a9a32c2eec2e4bba846a701ab572`
- Dataset hash: `7d75888200ab0425d6a88ca3d69bd6f160d06975a53c652f4bf09335a197bfbb`
- Recovery report hash: `cdbc2b6818a6152588248fe6d22def40b8a9ce8e08389e28480891f7cc42ccb0`
- Full dataset artifact: GitHub Actions run `37640825405`, artifact `11491682930`.
