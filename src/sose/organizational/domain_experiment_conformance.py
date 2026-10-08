from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
import json
from math import isfinite
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, InstanceOf

from .domain_experiment import ExperimentWorld, build_experiment_worlds
from .domain_experiment_runtime import (
    DomainExecutionRequest,
    DomainExperimentPlan,
    ExperimentObservation,
    RegimeReference,
)
from .domain_reference import (
    DomainReferenceDescriptor,
    GroundTruthClaim,
    GroundTruthTargetKind,
)


class _ConformanceReference(Protocol):
    descriptor: DomainReferenceDescriptor

    def build_model(self, point: dict[str, float]): ...

    def interventions(self): ...

    def execute(self, request: DomainExecutionRequest): ...

    def observation(self, result) -> ExperimentObservation: ...

    def ground_truth(self, world: ExperimentWorld) -> tuple[GroundTruthClaim, ...]: ...

    def classify_regime(self, world: ExperimentWorld) -> RegimeReference: ...

    def crn_signature(self, result) -> object: ...


class ConformanceCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    passed: bool
    detail: str = Field(min_length=1)

    def canonical_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class DomainExperimentConformance(BaseModel):
    """Gate-A domain experiment conformance report."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    descriptor_hash: str = Field(min_length=64, max_length=64)
    plan_hash: str = Field(min_length=64, max_length=64)
    checks: tuple[InstanceOf[ConformanceCheck], ...] = Field(min_length=1)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "descriptor_hash": self.descriptor_hash,
            "plan_hash": self.plan_hash,
            "checks": [
                check.canonical_payload()
                for check in sorted(self.checks, key=lambda item: item.name)
            ],
            "passed": self.passed,
        }

    @property
    def report_hash(self) -> str:
        return _canonical_hash(self.canonical_payload())


def run_domain_experiment_conformance(
    *,
    reference: _ConformanceReference,
    plan: DomainExperimentPlan,
) -> DomainExperimentConformance:
    """Evaluate reusable Gate-A invariants for an experiment-capable domain."""

    worlds_a = build_experiment_worlds(
        reference=reference,
        protocol=plan.protocol,
        agency_configurations=plan.agency_configurations,
        design_seed=plan.design_seed,
    )
    worlds_b = build_experiment_worlds(
        reference=reference,
        protocol=plan.protocol,
        agency_configurations=plan.agency_configurations,
        design_seed=plan.design_seed,
    )
    if not worlds_a:
        raise ValueError("conformance requires at least one experiment world")

    pre_execution_regimes = {
        world.world_hash: reference.classify_regime(world).canonical_payload()
        for world in worlds_a
    }
    checks = (
        _descriptor_integrity(reference, plan),
        _deterministic_world_construction(worlds_a, worlds_b),
        _exogenous_binding(reference.descriptor, worlds_a),
        _deterministic_execution(reference, plan, worlds_a),
        _crn_pairing(reference, plan, worlds_a),
        _observation_completeness(reference, plan, worlds_a),
        _ground_truth_integrity(reference, plan, worlds_a),
        _regime_purity(reference, worlds_a, pre_execution_regimes),
        _result_reproducibility(reference, plan, worlds_a),
    )
    return DomainExperimentConformance(
        descriptor_hash=reference.descriptor.descriptor_hash,
        plan_hash=plan.plan_hash,
        checks=checks,
    )


def _descriptor_integrity(
    reference: _ConformanceReference,
    plan: DomainExperimentPlan,
) -> ConformanceCheck:
    descriptor = reference.descriptor
    parameter_names = {item.name for item in descriptor.parameters}
    protocol_names = set(plan.protocol.parameter_ranges)
    capability_levels = {item.level for item in descriptor.agency_capabilities}
    requested_levels = set(plan.protocol.agency_levels)
    ok = protocol_names <= parameter_names and requested_levels <= capability_levels
    return ConformanceCheck(
        name="descriptor-integrity",
        passed=ok,
        detail=(
            "protocol axes and agency levels are declared by DomainReference"
            if ok
            else "protocol requests undeclared domain capabilities"
        ),
    )


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
    declared = {item.name for item in descriptor.parameters}
    ok = True
    for world in worlds:
        if not set(world.exogenous_parameters) <= declared:
            ok = False
            break
        for name, expected in world.exogenous_parameters.items():
            actual = world.model_spec.parameters.get(name)
            if (
                isinstance(actual, bool)
                or not isinstance(actual, (int, float))
                or not isfinite(float(actual))
                or float(actual) != expected
            ):
                ok = False
                break
        if not ok:
            break
    return ConformanceCheck(
        name="exogenous-binding",
        passed=ok,
        detail=(
            "every world preserves all configured exogenous coordinates in ModelSpec"
            if ok
            else "world/model exogenous binding mismatch detected"
        ),
    )


def _probe_worlds(worlds: tuple[ExperimentWorld, ...]) -> tuple[ExperimentWorld, ...]:
    by_path: dict[tuple[str, str], ExperimentWorld] = {}
    for world in worlds:
        key = (world.agency_configuration.level.value, world.arm_id)
        by_path.setdefault(key, world)
    return tuple(by_path[key] for key in sorted(by_path))


def _execute(
    reference: _ConformanceReference,
    plan: DomainExperimentPlan,
    world: ExperimentWorld,
):
    return reference.execute(
        DomainExecutionRequest(
            protocol=plan.protocol,
            world=world,
            root_seed=plan.root_seed,
            replication=0,
        )
    )


def _deterministic_execution(
    reference: _ConformanceReference,
    plan: DomainExperimentPlan,
    worlds: tuple[ExperimentWorld, ...],
) -> ConformanceCheck:
    ok = True
    for world in _probe_worlds(worlds):
        left = _execute(reference, plan, world)
        right = _execute(reference, plan, world)
        if left.evidence_hash != right.evidence_hash:
            ok = False
            break
    return ConformanceCheck(
        name="deterministic-execution",
        passed=ok,
        detail=(
            "representative agency executions reproduce identical evidence hashes"
            if ok
            else "equal execution inputs produced different evidence"
        ),
    )


def _crn_pairing(
    reference: _ConformanceReference,
    plan: DomainExperimentPlan,
    worlds: tuple[ExperimentWorld, ...],
) -> ConformanceCheck:
    by_design: dict[int, list[ExperimentWorld]] = defaultdict(list)
    for world in worlds:
        by_design[world.design_index].append(world)

    ok = True
    for design_index in sorted(by_design):
        candidates = sorted(
            by_design[design_index],
            key=lambda world: (
                world.agency_configuration.level.value,
                world.arm_id,
            ),
        )
        if len({world.crn_group for world in candidates}) != 1:
            ok = False
            break
        signatures = [
            reference.crn_signature(_execute(reference, plan, world))
            for world in candidates
        ]
        if signatures and any(
            signature != signatures[0] for signature in signatures[1:]
        ):
            ok = False
            break
        # One complete design point exercises every arm/agency latent pairing.
        break

    return ConformanceCheck(
        name="crn-pairing",
        passed=ok,
        detail=(
            "paired arms/agencies share CRN metadata and domain latent signatures"
            if ok
            else "declared CRN pairing does not preserve domain latent evidence"
        ),
    )


def _observation_completeness(
    reference: _ConformanceReference,
    plan: DomainExperimentPlan,
    worlds: tuple[ExperimentWorld, ...],
) -> ConformanceCheck:
    ok = True
    for world in _probe_worlds(worlds):
        observation = reference.observation(_execute(reference, plan, world))
        if not observation.metrics or any(
            not isfinite(value) for value in observation.metrics.values()
        ):
            ok = False
            break
    return ConformanceCheck(
        name="observation-completeness",
        passed=ok,
        detail=(
            "representative runs project to non-empty finite standard observations"
            if ok
            else "standard observation is empty or non-finite"
        ),
    )


def _ground_truth_integrity(
    reference: _ConformanceReference,
    plan: DomainExperimentPlan,
    worlds: tuple[ExperimentWorld, ...],
) -> ConformanceCheck:
    observations = {
        world.world_hash: reference.observation(_execute(reference, plan, world))
        for world in _probe_worlds(worlds)
    }
    ok = True
    for world in worlds:
        claims = reference.ground_truth(world)
        ids = [claim.claim_id for claim in claims]
        if not claims or len(ids) != len(set(ids)):
            ok = False
            break
        if any(
            claim.eligible and claim.ineligibility_reason is not None
            for claim in claims
        ):
            ok = False
            break
        if any(
            (not claim.eligible) and claim.ineligibility_reason is None
            for claim in claims
        ):
            ok = False
            break

        observation = observations.get(world.world_hash)
        if observation is not None and any(
            claim.eligible
            and claim.target_kind is GroundTruthTargetKind.METRIC
            and claim.target_name not in observation.metrics
            for claim in claims
        ):
            ok = False
            break

    return ConformanceCheck(
        name="ground-truth-integrity",
        passed=ok,
        detail=(
            "claims are unique, eligibility-valid, and eligible metrics observable"
            if ok
            else "ground-truth claims or eligible observation bindings are invalid"
        ),
    )


def _regime_purity(
    reference: _ConformanceReference,
    worlds: tuple[ExperimentWorld, ...],
    before: dict[str, dict[str, object]],
) -> ConformanceCheck:
    after = {
        world.world_hash: reference.classify_regime(world).canonical_payload()
        for world in worlds
    }
    ok = before == after
    return ConformanceCheck(
        name="regime-purity",
        passed=ok,
        detail=(
            "regime classification is a deterministic function of configured world mechanics"
            if ok
            else "regime classification changed for an unchanged world"
        ),
    )


def _result_reproducibility(
    reference: _ConformanceReference,
    plan: DomainExperimentPlan,
    worlds: tuple[ExperimentWorld, ...],
) -> ConformanceCheck:
    payloads: list[tuple[str, str, dict[str, object]]] = []
    for world in _probe_worlds(worlds):
        result = _execute(reference, plan, world)
        observation = reference.observation(result)
        payloads.append(
            (
                world.world_hash,
                result.evidence_hash,
                observation.canonical_payload(),
            )
        )
    first = _canonical_hash(payloads)

    repeated: list[tuple[str, str, dict[str, object]]] = []
    for world in _probe_worlds(worlds):
        result = _execute(reference, plan, world)
        observation = reference.observation(result)
        repeated.append(
            (
                world.world_hash,
                result.evidence_hash,
                observation.canonical_payload(),
            )
        )
    second = _canonical_hash(repeated)
    ok = first == second
    return ConformanceCheck(
        name="result-reproducibility",
        passed=ok,
        detail=(
            "representative canonical result payloads reproduce exactly"
            if ok
            else "canonical result payload changed across repeated execution"
        ),
    )


def _canonical_hash(payload: object) -> str:
    return sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
