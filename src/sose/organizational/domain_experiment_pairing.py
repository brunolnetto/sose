"""Paired-world and paired-run indexing for domain experiment analysis.

Split out of domain_experiment_analysis, which re-exports these helpers.
"""
from __future__ import annotations

from .agency import AgencyLevel
from .domain_experiment import ExperimentWorld
from .domain_experiment_runtime import ExperimentRunRecord


def _worlds_by_key(
    worlds: tuple[ExperimentWorld, ...],
) -> dict[tuple[int, str, AgencyLevel], ExperimentWorld]:
    by_key: dict[tuple[int, str, AgencyLevel], ExperimentWorld] = {}
    for world in worlds:
        key = (
            world.design_index,
            world.arm_id,
            world.agency_configuration.level,
        )
        if key in by_key:
            raise ValueError("duplicate experiment world identity")
        by_key[key] = world
    return by_key


def _runs_by_world(
    *,
    worlds: tuple[ExperimentWorld, ...],
    runs: tuple[ExperimentRunRecord, ...],
    replications: int,
) -> dict[str, dict[int, ExperimentRunRecord]]:
    expected_worlds = {world.world_hash for world in worlds}
    by_world: dict[str, dict[int, ExperimentRunRecord]] = {
        world_hash: {} for world_hash in expected_worlds
    }
    for run in runs:
        if run.world_hash not in by_world:
            raise ValueError("paired run evidence contains an unknown world")
        if run.replication in by_world[run.world_hash]:
            raise ValueError("paired run evidence contains duplicate replication identity")
        by_world[run.world_hash][run.replication] = run

    expected_indices = set(range(replications))
    if any(set(items) != expected_indices for items in by_world.values()):
        raise ValueError("paired run evidence must contain exact replication indices per world")
    return by_world


def _paired_values(
    control_runs: dict[int, ExperimentRunRecord],
    treatment_runs: dict[int, ExperimentRunRecord],
    metric_name: str,
) -> tuple[list[float], list[float], list[float]]:
    control_values: list[float] = []
    treatment_values: list[float] = []
    deltas: list[float] = []
    for replication in sorted(control_runs):
        control_run = control_runs[replication]
        treatment_run = treatment_runs[replication]
        if control_run.replication_seed != treatment_run.replication_seed:
            raise ValueError(
                "paired comparison requires matching replication seeds for every replication"
            )
        control_value = control_run.observation.metrics.get(metric_name)
        treatment_value = treatment_run.observation.metrics.get(metric_name)
        if control_value is None or treatment_value is None:
            raise ValueError(
                f"standard observation must expose preregistered metric {metric_name}"
            )
        control_values.append(control_value)
        treatment_values.append(treatment_value)
        deltas.append(treatment_value - control_value)
    return control_values, treatment_values, deltas
