from __future__ import annotations

from bisect import bisect_right
from math import ceil, isfinite
from statistics import fmean, median
from typing import Annotated, Iterable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .dataset import ObservedPRDataset


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DistributionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int = Field(ge=1)
    unit: NonBlankString
    mean: float = Field(allow_inf_nan=False)
    median: float = Field(allow_inf_nan=False)
    p90: float = Field(allow_inf_nan=False)


class LeadTimeValidation(BaseModel):
    """Held-out descriptive comparison; intentionally has no composite fit score."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_dataset_hash: str
    observed: DistributionSummary
    simulated: DistributionSummary
    ecdf_max_distance: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    mean_difference_seconds: float = Field(allow_inf_nan=False)
    median_difference_seconds: float = Field(allow_inf_nan=False)


def summarize_distribution(values: Iterable[float], *, unit: str) -> DistributionSummary:
    sample = _validated_sample(values)
    ordered = tuple(sorted(sample))
    rank = max(1, ceil(0.90 * len(ordered)))
    return DistributionSummary(
        count=len(ordered),
        unit=unit,
        mean=fmean(ordered),
        median=median(ordered),
        p90=ordered[rank - 1],
    )


def empirical_cdf_max_distance(left: Iterable[float], right: Iterable[float]) -> float:
    left_sample = tuple(sorted(_validated_sample(left)))
    right_sample = tuple(sorted(_validated_sample(right)))
    support = tuple(sorted(set(left_sample) | set(right_sample)))
    return max(
        abs(
            bisect_right(left_sample, value) / len(left_sample)
            - bisect_right(right_sample, value) / len(right_sample)
        )
        for value in support
    )


def compare_lead_time_distributions(
    *,
    observed: ObservedPRDataset,
    simulated_seconds: Iterable[float],
) -> LeadTimeValidation:
    observed_seconds: list[float] = []
    for trace in observed.traces:
        lead_time = trace.lead_time_seconds
        if lead_time is None:
            raise ValueError("lead-time validation requires a terminal-only observed dataset")
        observed_seconds.append(lead_time)

    simulated = _validated_sample(simulated_seconds)
    observed_summary = summarize_distribution(observed_seconds, unit="seconds")
    simulated_summary = summarize_distribution(simulated, unit="seconds")
    return LeadTimeValidation(
        observed_dataset_hash=observed.dataset_hash,
        observed=observed_summary,
        simulated=simulated_summary,
        ecdf_max_distance=empirical_cdf_max_distance(observed_seconds, simulated),
        mean_difference_seconds=simulated_summary.mean - observed_summary.mean,
        median_difference_seconds=simulated_summary.median - observed_summary.median,
    )


def _validated_sample(values: Iterable[float]) -> tuple[float, ...]:
    sample = tuple(float(value) for value in values)
    if not sample:
        raise ValueError("distribution sample must be non-empty")
    if any(not isfinite(value) or value < 0.0 for value in sample):
        raise ValueError("distribution values must be finite and non-negative")
    return sample
