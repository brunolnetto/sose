# Prospective PR-review v2 — Stage-1 model-freeze execution plan

Status: pre-freeze execution plan.

This document fixes the remaining Stage-1 fitting choices **before** the model-freeze
timestamp is created and before any Stage-2 item is eligible.

## Bound evidence

- Protocol: `pr-review-validation/v2`
- Corrected Stage-1 state hash:
  `ec96209137db9e46322cb2da572377b1b592e508f88515e881e8c875b1d87fd0`
- Corrected source snapshot hash:
  `23b35742cab04156460418056983d2c32ed68464fb8b503d681526d52ef422b3`
- Readiness checkpoint hash:
  `968bae3f1fefcb03a835c5b3e1d457e086c1ad057c9345337d66475a7cafa5a0`
- Training cohort: 18/18
- Holdout exposed: false

## Frozen fitting choices

- Model class: paired empirical item-delay analogs.
- Machine component: union of observed workflow-job active intervals clipped to
  `[opened_at, merged_at]`; overlapping jobs are not double counted.
- Residual component: elapsed lead time minus workflow-active union.
- Residual evidence class: Inferable; causal subtype unidentified.
- Stratification: only `author_is_bot`, observable at item creation.
- Outliers: retained.
- Actor effort/capacity/calendar inference from timestamp gaps: forbidden.
- Simulation RNG: counter-keyed.
- Simulation seed: `20261005` (reused from v1 to avoid seed shopping).
- Acceptance-bootstrap seed: `20261006`.
- Acceptance-bootstrap replicates: `4096`.
- Acceptance quantile: `0.95`.
- Stage-2 sample size used by acceptance bootstrap: the preregistered 12.
- Acceptance checks: separate absolute mean, median, p90 errors and ECDF distance.
- Composite fit score: none.
- Required reporting: mean, median, p90, maximum, ECDF distance, max-to-median ratio.

## Execution rule

The freeze timestamp is created only by the execution step after this plan is
published on an already-open pull request. The exact fit artifact, ModelSpec/hash,
acceptance criteria/hash, freeze artifact/hash, and frozen evidence state are then
persisted. The PR was opened before the freeze timestamp, so it remains interstitial
under the frozen cohort rule.

No Stage-2 outcome may alter any value above.
