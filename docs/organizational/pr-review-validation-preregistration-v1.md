# PR-review validation preregistration v1

Status: frozen analysis protocol. Machine-readable contract: `pr-review-validation-preregistration-v1.json`.

## Question

Can the already-defined A0 PR-review canonical, using only assumptions fixed before the holdout window, reproduce the total lead-time distribution of a later SOSE pull-request cohort within declared material-error bounds?

This is an external item-flow validation of the canonical. It is **not** a validation of reviewer effort, actor-time, CI-gate causality, context switching, meetings, or organizational agency.

## Study classification

This is a **retrospective pre-analysis protocol**, not a blinded prospective study.

The PR-review canonical (#268) and the human-side assumptions later reused by the empirical pipeline were established before the holdout cohort #279–#290. However, the holdout PRs had already completed and their public timestamps were available when this validation protocol and its acceptance thresholds were frozen. Therefore the result must not be described as prospectively preregistered or blinded.

A later study using PRs created only after this protocol is merged would be required for that stronger claim.

## Source window and split

The frozen source population is exactly SOSE PRs **#265–#290**, inclusive: 26 merged PRs.

The temporal split uses the existing `ObservedPRDataset.chronological_holdout()` contract with:

```text
holdout_fraction = 6 / 13 = 0.46153846153846156
```

Because `ceil(26 × 6/13) = 12`, the expected partition is:

- train: #265–#278 (14 PRs)
- holdout: #279–#290 (12 PRs)
- purged: none

The execution MUST fail rather than silently continue if the realized partition differs from this preregistered partition.

No holdout item may be removed after seeing prediction error. In particular, long-duration PRs remain part of the holdout.

## Data contract

For this study the analytical snapshot contains **only** the following item-level evidence:

- `created_at` → item opening;
- `merged_at` → terminal outcome;
- merged PRs only.

Workflow-job and review-event payloads MUST NOT be included in the v1 analytical snapshot, even if they are available from GitHub. This freezes the evidence path before execution and prevents optional gate evidence from changing the configured CI duration.

Therefore:

- reviewer service effort remains **Assumed**;
- reviewer capacity remains **Assumed**;
- rework probability remains **Assumed**;
- CI duration is always the frozen **Assumed** fallback of `120 s` for this study;
- no actor-time result may be presented as empirically calibrated.

The eligibility gate requires all 26 preregistered PRs and deliberately sets CI-coverage thresholds to zero. Those zero thresholds do not authorize optional CI evidence: workflow jobs are prohibited by the source contract for this protocol version. This is a lead-time validation decision, not evidence that CI or human mechanisms are observable.

A later protocol version may explicitly require independently identified gate provenance, but that would be a different study and MUST NOT be substituted into this v1 result.

## Frozen model assumptions

The study reuses the assumptions already exercised by the earlier empirical pilot rather than tuning them to the new holdout:

```text
reviewer_count             = 1
mean_review_time_seconds    = 300
mean_revision_time_seconds  = 180
rework_probability          = 0.15
fallback_ci_time_seconds    = 120
seed                        = 20261005
```

These values MUST NOT be changed after the held-out prediction is executed.

## Material validation bounds

The train cohort #265–#278 has a nearest-rank p90 lead time of **3597 seconds**. The time-domain validation limits are derived only from that pre-holdout scale:

- absolute mean error ≤ `0.50 × train_p90 = 1798.5 s`;
- absolute median error ≤ `0.25 × train_p90 = 899.25 s`;
- absolute p90 error ≤ `1.00 × train_p90 = 3597 s`.

The distribution-shape limit is frozen separately:

```text
ECDF max distance <= 0.35
```

The ECDF threshold is an explicit material-tolerance choice for this modest holdout, not an estimated confidence bound and not a fitted value.

The validation gate passes only if **every** requirement passes. No weighted or composite fit score is permitted.

## Refutation rule

For this study, the current A0 canonical fails external lead-time validation if any preregistered requirement exceeds its limit.

A failed result MUST remain a versioned artifact and MUST NOT trigger:

- removal of the offending PR;
- adjustment of assumptions;
- widening of validation thresholds;
- a second seed selected because it performs better;
- replacement of the canonical before reporting the failed result.

Model refinement may follow only as a subsequent study/version.

## Interpretation boundaries

Passing would mean only:

> Under the frozen assumptions and source contract, this A0 canonical reproduced this held-out SOSE PR lead-time cohort within the declared descriptive tolerances.

Failure would mean only:

> Under the frozen assumptions and source contract, this A0 canonical did not reproduce this held-out SOSE PR lead-time cohort within the declared descriptive tolerances.

Neither outcome establishes causal validity, organizational generality, or actor-level mechanism validity.

## Required execution artifact

The result PR MUST preserve at least:

- this protocol/version;
- source snapshot hash;
- train and holdout dataset hashes;
- exact train/holdout/purged keys;
- assumptions;
- seed;
- validation criteria hash;
- simulated lead times;
- per-metric validation checks;
- final pass/fail;
- empirical pilot artifact hash.

The result is reported unchanged whether it passes or fails.
