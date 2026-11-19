"""Pure (non-executing) Gate-A conformance checks (split from domain_experiment_conformance)."""
from __future__ import annotations

from collections import defaultdict
from math import isfinite

from pydantic import BaseModel, ConfigDict, Field

from .domain_experiment import ExperimentWorld
from .domain_reference import DomainReferenceDescriptor, GroundTruthTargetKind


class ConformanceCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    passed: bool
    detail: str = Field(min_length=1)

    def canonical_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def _deterministic_world_construction(
    left: tuple[ExperimentWorld, ...],
    right: tuple[ExperimentWorld, ...],
) -> ConformanceCheck:
    left_hashes = tuple(world.world_hash for world in left)
    right_hashes = tuple(world.world_hash for world in right)
    ok = left_hashes == right_hashes and len(set(left_hashes)) == len(left_hashes)
    return ConformanceCheck(
        name="deterministic-world-construction",
        passed=ok,
        detail=(
            f"{len(left_hashes)} unique worlds reproduce in identical order"
            if ok
            else "world construction is non-deterministic or duplicates identities"
        ),
    )


def _exogenous_binding(
    descriptor: DomainReferenceDescriptor,
    worlds: tuple[ExperimentWorld, ...],
) -> ConformanceCheck:
    """Verify DOE coordinates without confusing interventions with DOE corruption."""

    declared = {item.name for item in descriptor.parameters}
    by_design: dict[int, list[ExperimentWorld]] = defaultdict(list)
    for world in worlds:
        by_design[world.design_index].append(world)

    ok = all(_design_point_bound(by_design[index], declared) for index in sorted(by_design))

    return ConformanceCheck(
        name="exogenous-binding",
        passed=ok,
        detail=(
            "each design point is preserved across arms/agencies and binds the baseline ModelSpec"
            if ok
            else "DOE coordinates drift across worlds or fail baseline ModelSpec binding"
        ),
    )


def _design_point_bound(group: list[ExperimentWorld], declared: set[str]) -> bool:
    expected_point = dict(group[0].exogenous_parameters)
    if not set(expected_point) <= declared:
        return False
    # Framework-owned arm/agency expansion must carry the same sampled point
    # through every comparison world.
    if any(dict(world.exogenous_parameters) != expected_point for world in group):
        return False
    # Only the baseline ModelSpec must equal the raw DOE point. Treatment
    # ModelSpecs are expected to differ exactly because the framework applies
    # the declared ModelIntervention after building the sampled point model.
    baselines = [world for world in group if world.arm_id == "baseline"]
    if not baselines:
        return False
    return all(
        _binds_point(world.model_spec.parameters.get(name), expected)
        for world in baselines
        for name, expected in expected_point.items()
    )


def _binds_point(actual: object, expected: float) -> bool:
    return not (
        isinstance(actual, bool)
        or not isinstance(actual, (int, float))
        or not isfinite(float(actual))
        or float(actual) != expected
    )


def _claims_valid(claims, observation) -> bool:
    ids = [claim.claim_id for claim in claims]
    if not claims or len(ids) != len(set(ids)):
        return False
    if not _eligibility_consistent(claims):
        return False
    return observation is None or _eligible_metrics_observed(claims, observation)


def _eligibility_consistent(claims) -> bool:
    if any(
        claim.eligible and claim.ineligibility_reason is not None
        for claim in claims
    ):
        return False
    return not any(
        (not claim.eligible) and claim.ineligibility_reason is None
        for claim in claims
    )


def _eligible_metrics_observed(claims, observation) -> bool:
    return not any(
        claim.eligible
        and claim.target_kind is GroundTruthTargetKind.METRIC
        and claim.target_name not in observation.metrics
        for claim in claims
    )
