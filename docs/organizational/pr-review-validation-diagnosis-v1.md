# PR-review validation diagnosis v1

This document is a **post-result descriptive diagnosis** of the immutable result in `pr-review-validation-result-v1.json`. It does not modify the source population, holdout, assumptions, seed, acceptance limits, or pass/fail outcome.

## Frozen outcome

The original gate failed only `abs_mean_difference_seconds`:

- absolute mean error: **2,983.785 s** > 1,798.5 s;
- absolute median error: **27.380 s** <= 899.25 s;
- absolute p90 error: **923.114 s** <= 3,597.0 s;
- ECDF distance: **0.333333** <= 0.35.

The diagnostic therefore concerns why the observed distribution contains substantially more upper-tail elapsed time than the current canonical produces.

## Tail concentration

The largest observed holdout item is **SOSE PR #287**:

- observed lead time: **35,316 s**;
- share of all observed holdout lead time: **85.03%**;
- observed max / median ratio: **70.28×**;
- simulated max / median ratio: **1.45×**;
- observed-max minus simulated-max gap: **34,548.917 s**.

PR #287 remains valid observed evidence under the frozen source contract. These diagnostics are **not** a leave-one-out acceptance calculation and MUST NOT be used to reinterpret the v1 gate as passing.

## What the failure says

The v1 canonical uses assumed actor-side service parameters and produces a comparatively narrow lead-time distribution. The observed holdout contains an hours-long elapsed-time tail that the canonical does not generate.

This supports a narrow conclusion:

> The frozen v1 PR-review configuration is not adequate for the observed mean/tail behavior of this holdout.

It does **not** identify the causal mechanism behind the tail.

## Candidate mechanisms for a follow-up study

The following are hypotheses, not findings from the v1 source snapshot:

- calendar / off-hours waiting;
- reviewer or decision-resource availability;
- asynchronous information waiting;
- CI or external dependency waiting;
- heterogeneous work size;
- heavy-tailed service requirements;
- explicit blocked states.

The v1 snapshot contains only PR open/merge timestamps, so it cannot distinguish among these mechanisms. A follow-up must specify richer observable evidence and freeze its analysis plan separately before evaluating a new holdout.

## Guardrail

The executable diagnostic API exposes `acceptance_unchanged = true` and reports the original failing metrics directly from the immutable execution artifact. It intentionally provides no function for dropping the largest item, recomputing the gate on a subset, or retuning the frozen model.
