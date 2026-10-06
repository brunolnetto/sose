# PR-review external validation result v1

**Protocol:** `pr-review-validation/v1`  
**Study type:** retrospective pre-analysis  
**Source population:** merged SOSE PRs #265–#290  
**Train:** #265–#278  
**Holdout:** #279–#290  
**Purged:** none  

This document reports the first execution of the frozen protocol in `pr-review-validation-preregistration-v1.json`. No holdout item was removed, and no seed, assumption, split, or acceptance threshold was retuned after observing the result.

## Result

The preregistered acceptance gate **failed**.

| Requirement | Observed discrepancy | Frozen limit | Result |
|---|---:|---:|---|
| absolute mean lead-time difference | 2,983.785 s | 1,798.5 s | **FAIL** |
| absolute median lead-time difference | 27.380 s | 899.25 s | PASS |
| absolute p90 lead-time difference | 923.114 s | 3,597.0 s | PASS |
| ECDF maximum distance | 0.333333 | 0.35 | PASS |

Observed holdout mean lead time was **3,461.25 s**; simulated mean was **477.465 s**. The main discrepancy is therefore in the mean, not the median, p90 threshold, or ECDF gate.

The holdout retains PR #287, whose observed lead time is 35,316 s. The protocol explicitly required retaining outliers; it is therefore part of the failed result rather than grounds for post-hoc exclusion.

## Interpretation boundary

This failure does **not** establish that the Organizational Dynamics framework is invalid. It rejects this frozen PR-review model/assumption configuration against this retrospective holdout and acceptance rule.

Human review effort, reviewer capacity, revision effort, rework probability, and CI gate time remain assumed under the source contract. The source snapshot contains PR open/merge timestamps only and does not infer actor-time from elapsed gaps.

The scientifically valid next step is diagnosis and a separately preregistered follow-up, not modification of this result.

## Reproducibility

- source snapshot: `pr-review-validation-source-v1.json`
- execution plan: `pr-review-validation-plan-v1.json`
- complete machine-readable result: `pr-review-validation-result-v1.json`
- snapshot hash: `339c5df588e5afac6788712ba21bbb516f8569dcd139e49cfbde4a325ae140b5`
- plan hash: `dc886cf185f51e675542fbba1111c19bb421cdb0c755eecf4a74056c4ba77142`
- execution hash: `406143ff97a17cf89b78ac92e2d081d7866d361e388055c106a5df8c62345d2d`
