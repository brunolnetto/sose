"""OD-5a crossed, exogenous Sobol design falsification; no science worlds run."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace

import pytest

from sose.organizational.experiment import ParameterRange
from sose.organizational.od5a_design import (
    ARMS, LOAD_GRID, N_REPLICATIONS, N_RUN_KEYS, N_STRATA, N_WORLDS,
    NONLOAD_COORDINATES, build_od5a_configured_worlds, iter_od5a_run_keys,
)


def _bounds():
    return {name: ParameterRange(low=0.25, high=0.75)
            for name in NONLOAD_COORDINATES}


def test_registered_crossed_worlds_share_nonload_coordinates_over_all_loads():
    worlds = build_od5a_configured_worlds(nonload_bounds=_bounds(), design_seed=100)
    assert len(worlds) == N_WORLDS == 224
    assert len({world.world_hash for world in worlds}) == 224
    assert len({world.crn_group for world in worlds}) == 224
    by_stratum = {}
    for world in worlds:
        by_stratum.setdefault(world.stratum_index, []).append(world)
        assert set(world.nonload_coordinates) == NONLOAD_COORDINATES
        assert all(0.25 <= x < 0.75 for x in world.nonload_coordinates.values())
    assert len(by_stratum) == N_STRATA == 32
    for group in by_stratum.values():
        assert [world.rho_nominal for world in group] == list(LOAD_GRID)
        assert len({tuple(sorted(world.nonload_coordinates.items())) for world in group}) == 1
    assert [w.load_index for w in worlds[:7]] == list(range(7))


def test_design_is_order_independent_deterministic_and_seed_bound():
    a = build_od5a_configured_worlds(nonload_bounds=_bounds(), design_seed=9)
    b = build_od5a_configured_worlds(
        nonload_bounds=dict(reversed(list(_bounds().items()))),
        design_seed=9,
    )
    assert tuple(w.world_hash for w in a) == tuple(w.world_hash for w in b)
    c = build_od5a_configured_worlds(nonload_bounds=_bounds(), design_seed=10)
    assert tuple(w.world_hash for w in a) != tuple(w.world_hash for w in c)


def test_registered_crn_reuses_world_identity_across_arms_but_not_replication_keys():
    worlds = build_od5a_configured_worlds(nonload_bounds=_bounds(), design_seed=1)
    keys = tuple(iter_od5a_run_keys(worlds))
    assert len(keys) == N_RUN_KEYS == 17920
    assert len({key.run_hash for key in keys}) == len(keys)
    first = [key for key in keys if key.world_hash == worlds[0].world_hash]
    assert len(first) == len(ARMS) * N_REPLICATIONS
    assert {key.crn_group for key in first} == {worlds[0].crn_group}
    assert Counter(key.arm_id for key in first) == dict.fromkeys(ARMS, N_REPLICATIONS)
    assert {key.replication_index for key in first} == set(range(N_REPLICATIONS))
    assert worlds[0].crn_group != worlds[1].crn_group


@pytest.mark.parametrize("fault,match", [
    ("missing", "exact non-load bounds"),
    ("extra_rho", "exact non-load bounds"),
    ("outcome", "exact non-load bounds"),
    ("non_numeric", "valid ParameterRange"),
    ("bad_seed", "nonnegative integer"),
])
def test_exogenous_design_refuses_missing_unknown_or_endogenous_axes(fault, match):
    bounds = _bounds()
    seed = 42
    if fault == "missing":
        del bounds[next(iter(bounds))]
    elif fault == "extra_rho":
        bounds["rho_nominal"] = ParameterRange(low=0.4, high=1.6)
    elif fault == "outcome":
        bounds["realized_WIP"] = ParameterRange(low=0.0, high=1.0)
    elif fault == "non_numeric":
        bounds[next(iter(bounds))] = "arbitrary observed outcome"
    else:
        seed = -1
    with pytest.raises(ValueError, match=match):
        build_od5a_configured_worlds(nonload_bounds=bounds, design_seed=seed)


@pytest.mark.parametrize("fault,match", [
    ("truncated", "exactly 224"),
    ("duplicated", "incomplete or duplicates"),
    ("load_changed", "load coordinate differs"),
    ("stratum_changed", "stratum changes"),
    ("mixed_seed", "mixes unrelated"),
    ("permuted", "canonical stratum/load order"),
    ("outcome_injected", "invalid non-load coordinates"),
])
def test_incomplete_or_confounded_world_matrix_cannot_expand_into_run_keys(fault, match):
    worlds = list(build_od5a_configured_worlds(
        nonload_bounds=_bounds(), design_seed=42,
    ))
    if fault == "truncated":
        worlds.pop()
    elif fault == "duplicated":
        worlds[1] = worlds[0]
    elif fault == "load_changed":
        worlds[0] = replace(worlds[0], rho_nominal=1.6)
    elif fault == "stratum_changed":
        point = dict(worlds[1].nonload_coordinates)
        point["calendar_availability"] += 0.01
        worlds[1] = replace(worlds[1], nonload_coordinates=point)
    elif fault == "mixed_seed":
        worlds[1] = replace(worlds[1], design_seed=3)
    elif fault == "permuted":
        worlds.reverse()
    elif fault == "outcome_injected":
        point = dict(worlds[1].nonload_coordinates)
        point["realized_WIP"] = 1.0
        worlds[1] = replace(worlds[1], nonload_coordinates=point)
    with pytest.raises(ValueError, match=match):
        tuple(iter_od5a_run_keys(worlds))


def test_world_hash_contains_structural_coordinates_before_any_observation():
    worlds = build_od5a_configured_worlds(nonload_bounds=_bounds(), design_seed=5)
    first = worlds[0]
    assert first.canonical_payload["protocol"] == "od5a-structure-capacity-v2"
    assert first.canonical_payload["rho_nominal"] == 0.4
    changed = replace(first, rho_nominal=1.6)
    assert changed.world_hash != first.world_hash
    with pytest.raises(TypeError):
        first.nonload_coordinates["dependency_density"] = 0.9

def test_configured_world_defensively_copies_mutable_exogenous_coordinates():
    worlds = build_od5a_configured_worlds(nonload_bounds=_bounds(), design_seed=17)
    user_mapping = dict(worlds[0].nonload_coordinates)
    world = replace(worlds[0], nonload_coordinates=user_mapping)
    frozen_hash = world.world_hash
    user_mapping["calendar_availability"] += 0.1
    assert world.world_hash == frozen_hash
    with pytest.raises(TypeError):
        world.nonload_coordinates["calendar_availability"] = 0.9


def test_run_enumeration_rejects_boolean_exogenous_coordinate():
    worlds = list(build_od5a_configured_worlds(nonload_bounds=_bounds(), design_seed=17))
    altered = dict(worlds[0].nonload_coordinates)
    altered["calendar_availability"] = True
    worlds[0] = replace(worlds[0], nonload_coordinates=altered)
    with pytest.raises(ValueError, match="invalid non-load coordinates"):
        tuple(iter_od5a_run_keys(worlds))
