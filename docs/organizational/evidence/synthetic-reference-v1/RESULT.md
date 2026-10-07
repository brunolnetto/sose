# Synthetic organizational reference experiment v1

This is a **verification experiment against known synthetic generating mechanics**.
It is not empirical validation and uses no observed organizational outcomes.

## Fixed design

- 12 Latin-hypercube design points.
- Three arms per design point: baseline, capacity expansion, and rework-reducing automation.
- 36 worlds total, 16 CRN replications each, 576 replicated runs.
- Fixed design and root seed: `20261007`.
- Regime coordinates use exogenous inputs only: arrival rate, service capacity, service CV, rework probability, and transition cost.
- WIP, throughput, queueing delay, and lead time are outputs only.

## Result

- Stable worlds: **22 / 36**.
- Saturated worlds: **14 / 36**.
- Mean relative error versus stationary M/G/1 ground truth in stable worlds: **11.19%**.
- Direction recovery for eligible non-null intervention effects: **7 / 7 = 100%**.
- Null-region effects: **1**.
- Regime transitions across intervention arms:
  - stable → stable: **8**
  - saturated → stable: **10**
  - saturated → saturated: **6**
  - stable → saturated: **0**

## Interpretation

The reference generator recovers intervention direction perfectly for every eligible non-null effect in this design. The analytical lead-time discrepancy is concentrated near the critical boundary: for stable worlds with offered load below 0.70, mean relative error is approximately **2.14%**; worlds with offered load at or above 0.95 dominate the aggregate error because finite-horizon queue backlogs converge slowly near saturation.

This result supports the intended claim: SOSE can synthesize organizational process data from explicit exogenous mechanics and recover known regime/intervention structure without using endogenous outputs as DOE coordinates.

The result does **not** establish empirical validity for real organizations, A1/A2 agency behavior, or transient intervention dynamics. Those remain separate experiments.
