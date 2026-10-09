# Manufacturing Organizational Experiment v1 — Official Synthetic Results

**Evidence status:** Official frozen-protocol execution completed; synthetic verification only.  
**Domain:** Manufacturing PC5 reference; A0 fixed agency only.  
**Frozen plan SHA-256:** `4b972a4d0b86560718bc7f2d50ebf89b5f729c892681768fe89bd72a181a8911`  
**Frozen protocol SHA-256:** `8983bef05b5a2803f0f32be510790aa6f7ea9c453333e4a79449b186b1281d0d`  
**Result SHA-256:** `f79bd89fc50ca5fadc0788c657cc752d1291dd464fbf1643e660a67667c4338b`  
**Execution:** [GitHub Actions run 37866978379](https://github.com/brunolnetto/sose/actions/runs/37866978379)  
**Immutable evidence artifact:** `manufacturing-v1-official-evidence` (artifact ID `11588497098`; GitHub retention 90 days).  
**Executed checkout SHA:** `51f2fdcfa120187a8f9ba7bf69ffe32ebc770b2f`. This is the PR merge-ref checkout, not the original preregistration commit.

## Frozen design and observed cardinalities

- Numeric exogenous DOE axis: `quantity` in [1, 1000].
- Six Latin Hypercube points, seeds `20261008` (design and root).
- Three arms: `baseline`, `machine_downtime`, and `yield_degradation`.
- A0 fixed agency, two replications per world; 18 worlds, 36 runs.
- 84 paired intervention metric contrasts, **24 eligible** and **60 explicitly ineligible**.
- 216 eligible typed ground-truth assessments, **216 passed**.
- 36 raw evidence records exported. All seven canonical evidence files match the SHA-256 hashes in the evidence manifest.

The 24 eligible effect contrasts correspond to six design points for each of the four preregistered metric/arm combinations. Ineligible contrasts remain in the record; they cannot be used as claims about effects.

## Results

| Arm | Average lead time (s) | Average output quantity | Yield ratio | Breakdowns/run | Machine reacquired |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline | 3,600 | 492.89349 | 1.0 | 0 | No |
| Machine downtime | 10,800 | 492.89349 | 1.0 | 1 | Yes |
| Yield degradation | 3,600 | 394.31479 | 0.8 | 0 | No |

| Eligible paired contrast | Mean treatment minus baseline | Across-six-design-point direction |
| --- | ---: | --- |
| Downtime: lead time | +7,200 seconds | 6/6 positive |
| Downtime: breakdown count | +1 | 6/6 positive |
| Yield degradation: output quantity | −98.57870 | 6/6 negative |
| Yield degradation: yield ratio | −0.2 | 6/6 negative |

All 36 runs completed. The six typed assessment identifiers (`mfg.completed`, `mfg.material-issued-once`, `mfg.raw-material-consumed`, `mfg.wip-released`, `mfg.yield-ratio`, `mfg.breakdown-count`) each passed in all 36 runs. This is evidence for these exported checks, not proof that every conceivable Manufacturing invariant is covered.

## Scientific interpretation

The synthetic execution recovered the configured directions for machine downtime and yield degradation. It also preserved recorded process invariants under the official A0 configuration. The downtime arm includes durable breakdown/preemption evidence, machine reacquisition, and a finite two-hour lead-time penalty. The yield arm records a 20% reduction in `yield_ratio` and paired output quantity.

**Do not confuse expected adverse intervention effects with a beneficial optimization.** The generic report's `favorable=False` for these contrasts correctly describes decreased business performance, while the preregistered verification question asks whether the adverse mechanics are recovered.

### Inferential limitation

Paired standard error is **0 for all 24 eligible contrasts**. The two deterministic replications per world therefore do not provide an informative estimate of stochastic uncertainty. Any reported zero-width 95% confidence interval is a degenerate feature of the deterministic reference experiment, **not** a statistically justified real-world precision claim. The six DOE points characterize the designed synthetic configurations, not independently sampled organizations.

### Claims not supported

- No empirical or external validity claim, calibration to observed factory data, or real-world effect-size forecast.
- No A1/A2 managerial/actor adaptation claim; the protocol is A0 only.
- No stationarity, queueing, congestion, demand surge, or organizational regime-map claim.
- No causal effect beyond the specified deterministic intervention implementation and experimental design.
- No inference that these 36 runs establish maturity of other domains or complete SOSE 1.0 Gate 5.

## Reproducibility and provenance

The runner rejects any changes to the frozen manifest Git blob or its named dependencies and verifies the exact executable protocol and plan hashes before running a world. Each exported raw-evidence hash is verified before publication; outputs are staged and never overwrite an existing destination. The artifact contains `worlds.json`, `runs.json`, `evidence.json`, `references.json`, `assessments.json`, `effects.json`, `result-manifest.json`, `evidence-manifest.json`, Python version, installed packages, and executed checkout SHA.

To regenerate from an eligible checkout:

```bash
python -m sose.organizational.manufacturing_official_run \
  --root . \
  --output-dir artifacts/manufacturing-v1-fresh
```

The destination must not already exist. Compare the new result hash to the official result above; also verify file-level hashes for bitwise evidence equivalence. This document records the observed official output and must not be used to modify or retune the frozen protocol after inspection.
