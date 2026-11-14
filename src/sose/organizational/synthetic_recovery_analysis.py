"""Recovery of the known A0 queueing ground truth (split from synthetic_analysis).

Regime coordinates come only from configured exogenous parameters. Observed WIP,
throughput, and delays remain outcomes and are never fed back into regime identity.
"""
from __future__ import annotations

from statistics import fmean

from .experiment import ExperimentProtocol
from .synthetic_analysis import (
    AnalyticalWorldReference,
    SyntheticEffectRecovery,
    SyntheticRecoveryReport,
    SyntheticWorldRecovery,
    _effect_direction,
    _regime_transition,
    analytical_world_reference,
)
from .synthetic_runner import SyntheticExperimentDataset
from .synthetic_study import SyntheticWorldSpec


def recover_reference_synthetic_experiment(
    *,
    protocol: ExperimentProtocol,
    worlds: tuple[SyntheticWorldSpec, ...],
    dataset: SyntheticExperimentDataset,
) -> SyntheticRecoveryReport:
    world_by_hash, equivalence_margin = _validated_bindings(protocol, worlds, dataset)
    runs_by_world: dict[str, list[object]] = {world_hash: [] for world_hash in world_by_hash}
    for run in dataset.runs:
        runs_by_world[run.world_hash].append(run)

    world_recoveries: list[SyntheticWorldRecovery] = []
    reference_by_hash: dict[str, AnalyticalWorldReference] = {}
    for world in worlds:
        reference = analytical_world_reference(world)
        reference_by_hash[world.world_hash] = reference
        world_recoveries.append(_world_recovery(
            world, reference, runs_by_world[world.world_hash], dataset.replications,
        ))

    baseline_by_key = {
        (world.design_index, world.agency_level.value): world
        for world in worlds
        if world.arm_id == "baseline"
    }
    effects: list[SyntheticEffectRecovery] = []
    for world in worlds:
        if world.arm_id == "baseline":
            continue
        baseline = baseline_by_key.get((world.design_index, world.agency_level.value))
        if baseline is None:
            raise ValueError("each intervention world requires a paired baseline world")
        effects.append(_effect_recovery(
            world, baseline, reference_by_hash, runs_by_world, equivalence_margin,
        ))
    return _recovery_report(protocol, dataset, worlds, world_recoveries, effects)


def _validated_bindings(protocol, worlds, dataset) -> tuple[dict, float]:
    if dataset.protocol_hash != protocol.protocol_hash:
        raise ValueError("dataset protocol hash does not match experiment protocol")
    if any(world.agency_level.value != "A0" for world in worlds):
        raise ValueError("reference synthetic analytical recovery supports A0 mechanics only")
    if any(world.intervention_transition_time != 0.0 for world in worlds):
        raise ValueError(
            "stationary synthetic recovery excludes intervention transition downtime"
        )
    return _validated_world_binding(protocol, worlds, dataset), _lead_time_margin(protocol)


def _validated_world_binding(protocol, worlds, dataset) -> dict:
    world_by_hash = {world.world_hash: world for world in worlds}
    run_world_hashes = {run.world_hash for run in dataset.runs}
    if len(world_by_hash) != len(worlds) or run_world_hashes != set(world_by_hash):
        raise ValueError("synthetic dataset/world binding is incomplete")
    if any(world.protocol_hash != protocol.protocol_hash for world in worlds):
        raise ValueError("synthetic world binding does not match experiment protocol")
    return world_by_hash


def _lead_time_margin(protocol) -> float:
    lead_outcome = next(
        (outcome for outcome in protocol.outcomes if outcome.name == "lead_time"),
        None,
    )
    if lead_outcome is None:
        raise ValueError("synthetic recovery requires a preregistered lead_time outcome")
    return lead_outcome.equivalence_margin


def _world_recovery(world, reference, runs, replications: int) -> SyntheticWorldRecovery:
    if len(runs) != replications:
        raise ValueError("each synthetic world must have exactly the declared replications")
    replication_indices = {run.replication for run in runs}
    if replication_indices != set(range(replications)):
        raise ValueError(
            "each synthetic world must bind exact replication indices 0..R-1"
        )
    observed_lead = fmean(run.mean_lead_time for run in runs)
    observed_throughput = fmean(run.throughput for run in runs)
    observed_wip = fmean(run.mean_wip for run in runs)
    if reference.expected_mean_lead_time is None:
        relative_error = None
    else:
        relative_error = abs(
            observed_lead - reference.expected_mean_lead_time
        ) / reference.expected_mean_lead_time
    return SyntheticWorldRecovery(
        world_hash=world.world_hash,
        design_index=world.design_index,
        arm_id=world.arm_id,
        agency_level=world.agency_level.value,
        offered_load=reference.offered_load,
        stable=reference.stable,
        expected_mean_lead_time=reference.expected_mean_lead_time,
        observed_mean_lead_time=observed_lead,
        observed_mean_throughput=observed_throughput,
        observed_mean_wip=observed_wip,
        relative_error_mean_lead_time=relative_error,
    )


def _paired_mean_delta(intervention_runs: dict, baseline_runs: dict, attribute: str) -> float:
    return fmean(
        getattr(intervention_runs[index], attribute) - getattr(baseline_runs[index], attribute)
        for index in sorted(baseline_runs)
    )


def _expected_lead_effect(baseline_reference, intervention_reference, simulated_delta, margin):
    """(expected delta, null region, direction recovered); all undefined if unstable."""
    if (
        baseline_reference.expected_mean_lead_time is None
        or intervention_reference.expected_mean_lead_time is None
    ):
        return None, False, None
    expected_delta = (
        intervention_reference.expected_mean_lead_time
        - baseline_reference.expected_mean_lead_time
    )
    null_region = abs(expected_delta) <= margin
    direction_recovered = (
        _effect_direction(simulated_delta, margin)
        == _effect_direction(expected_delta, margin)
    )
    return expected_delta, null_region, direction_recovered


def _effect_recovery(world, baseline, reference_by_hash, runs_by_world, margin) -> SyntheticEffectRecovery:
    baseline_reference = reference_by_hash[baseline.world_hash]
    intervention_reference = reference_by_hash[world.world_hash]
    baseline_runs = {run.replication: run for run in runs_by_world[baseline.world_hash]}
    intervention_runs = {run.replication: run for run in runs_by_world[world.world_hash]}
    if set(baseline_runs) != set(intervention_runs):
        raise ValueError("paired synthetic effects require matching CRN replications")
    simulated_delta = _paired_mean_delta(intervention_runs, baseline_runs, "mean_lead_time")
    simulated_delta_throughput = _paired_mean_delta(intervention_runs, baseline_runs, "throughput")
    simulated_delta_cost = _paired_mean_delta(
        intervention_runs, baseline_runs, "total_operating_cost",
    )
    expected_delta_throughput = (
        intervention_reference.expected_throughput
        - baseline_reference.expected_throughput
    )
    regime_transition = _regime_transition(
        baseline_reference.stable,
        intervention_reference.stable,
    )
    expected_delta, null_region, direction_recovered = _expected_lead_effect(
        baseline_reference, intervention_reference, simulated_delta, margin,
    )
    return SyntheticEffectRecovery(
        design_index=world.design_index,
        agency_level=world.agency_level.value,
        arm_id=world.arm_id,
        baseline_world_hash=baseline.world_hash,
        intervention_world_hash=world.world_hash,
        regime_transition=regime_transition,
        expected_delta_mean_lead_time=expected_delta,
        simulated_delta_mean_lead_time=simulated_delta,
        expected_delta_throughput=expected_delta_throughput,
        simulated_delta_throughput=simulated_delta_throughput,
        simulated_delta_operating_cost=simulated_delta_cost,
        null_region=null_region,
        direction_recovered=direction_recovered,
    )


def _recovery_report(protocol, dataset, worlds, world_recoveries, effects) -> SyntheticRecoveryReport:
    stable_errors = [
        world.relative_error_mean_lead_time
        for world in world_recoveries
        if world.relative_error_mean_lead_time is not None
    ]
    eligible_directions = [
        effect.direction_recovered
        for effect in effects
        if effect.direction_recovered is not None and not effect.null_region
    ]
    return SyntheticRecoveryReport(
        protocol_hash=protocol.protocol_hash,
        dataset_hash=dataset.dataset_hash,
        world_count=len(worlds),
        stable_world_count=sum(world.stable for world in world_recoveries),
        saturated_world_count=sum(not world.stable for world in world_recoveries),
        mean_relative_error_stable=(
            fmean(stable_errors) if stable_errors else 0.0
        ),
        direction_recovery_rate=_direction_recovery_rate(eligible_directions),
        worlds=tuple(world_recoveries),
        effects=tuple(effects),
    )


def _direction_recovery_rate(eligible_directions: list) -> float:
    if not eligible_directions:
        return 1.0
    return sum(bool(value) for value in eligible_directions) / len(eligible_directions)
