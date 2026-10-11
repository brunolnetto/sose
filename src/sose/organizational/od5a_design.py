"""Prospectively registered OD-5a crossed exogenous design (not a simulator).

Generate exactly 32 Sobol *non-load* strata crossed with all seven registered
nominal offered-load coordinates. Identity is arm-independent to preserve the
intended counter-CRN pairing under a later separately frozen runner.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite
from types import MappingProxyType

from .experiment import ParameterRange, SamplingDesign
from .sampling import sample_parameter_space


NONLOAD_COORDINATES = frozenset((
    "calendar_availability",
    "dependency_density",
    "routing_complexity",
    "nominal_service_distribution",
    "nominal_coordination_latency",
    "declared_intervention_cost_ratio",
))
# This is a *configured* ratio to baseline nominal capacity, never realized utilization.
LOAD_GRID = (0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6)
ARMS = (
    "baseline", "capacity_only", "topology_only", "coordination_only",
    "structural_combined",
)
N_STRATA = 32
N_REPLICATIONS = 16
N_WORLDS = N_STRATA * len(LOAD_GRID)
N_RUN_KEYS = N_WORLDS * N_REPLICATIONS * len(ARMS)


def _hash(payload: object) -> str:
    return sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OD5aConfiguredWorld:
    design_seed: int
    stratum_index: int
    load_index: int
    rho_nominal: float
    nonload_coordinates: Mapping[str, float]

    def __post_init__(self) -> None:
        # frozen=True does not freeze an externally supplied mutable Mapping.
        # A caller must not be able to change a hash-addressed world in place.
        object.__setattr__(
            self, "nonload_coordinates",
            MappingProxyType(dict(self.nonload_coordinates)),
        )

    @property
    def canonical_payload(self) -> dict[str, object]:
        return {
            "protocol": "od5a-structure-capacity-v2",
            "design_seed": self.design_seed,
            "stratum_index": self.stratum_index,
            "load_index": self.load_index,
            "rho_nominal": self.rho_nominal,
            "nonload_coordinates": dict(sorted(self.nonload_coordinates.items())),
        }

    @property
    def world_hash(self) -> str:
        return _hash(self.canonical_payload)

    @property
    def crn_group(self) -> str:
        # Shared by all five arms and all replicas for a *fixed* exogenous world.
        # Each replicate receives its independent counter-CRN indexed draw.
        return f"od5a-{self.world_hash}"


@dataclass(frozen=True, slots=True)
class OD5aRunKey:
    world_hash: str
    crn_group: str
    arm_id: str
    replication_index: int

    @property
    def run_hash(self) -> str:
        return _hash({
            "world_hash": self.world_hash,
            "crn_group": self.crn_group,
            "arm_id": self.arm_id,
            "replication_index": self.replication_index,
        })


def build_od5a_configured_worlds(
    *, nonload_bounds: Mapping[str, ParameterRange], design_seed: int,
) -> tuple[OD5aConfiguredWorld, ...]:
    """Sample configured strata only; outcomes cannot influence selection.

    All bounds and the seed are supplied by the *future* execution freeze,
    not opportunistically chosen inside the experiment engine.
    """
    if set(nonload_bounds) != NONLOAD_COORDINATES:
        missing = sorted(NONLOAD_COORDINATES - set(nonload_bounds))
        extra = sorted(set(nonload_bounds) - NONLOAD_COORDINATES)
        raise ValueError(f"OD-5a requires exact non-load bounds; missing={missing}; extra={extra}")
    if isinstance(design_seed, bool) or not isinstance(design_seed, int) or design_seed < 0:
        raise ValueError("design_seed must be a nonnegative integer")
    if not all(isinstance(bounds, ParameterRange) for bounds in nonload_bounds.values()):
        raise ValueError("each non-load dimension requires a valid ParameterRange")
    points = sample_parameter_space(
        parameter_ranges=nonload_bounds,
        design=SamplingDesign.SOBOL,
        sample_size=N_STRATA,
        seed=design_seed,
    )
    if len(points) != N_STRATA:
        raise RuntimeError("OD-5a Sobol stratum count contradicts preregistration")
    result = []
    for stratum, point in enumerate(points):
        if any(not isfinite(x) for x in point.values()):
            raise RuntimeError("non-finite OD-5a exogenous coordinate")
        shared = MappingProxyType(dict(sorted(point.items())))
        for load_index, rho in enumerate(LOAD_GRID):
            result.append(OD5aConfiguredWorld(
                design_seed=design_seed,
                stratum_index=stratum,
                load_index=load_index,
                rho_nominal=rho,
                nonload_coordinates=shared,
            ))
    if len({world.world_hash for world in result}) != N_WORLDS:
        raise RuntimeError("OD-5a configured design contains repeated worlds")
    return tuple(result)


def iter_od5a_run_keys(
    worlds: Sequence[OD5aConfiguredWorld],
) -> Iterator[OD5aRunKey]:
    """Enumerate the locked 17,920 run *identities* without executing any."""
    if len(worlds) != N_WORLDS:
        raise ValueError("OD-5a requires exactly 224 configured worlds")
    expected = {(stratum, i) for stratum in range(N_STRATA) for i in range(len(LOAD_GRID))}
    actual = {(world.stratum_index, world.load_index) for world in worlds}
    if len(actual) != len(worlds) or actual != expected:
        raise ValueError("OD-5a design is incomplete or duplicates a stratum/load pair")
    if tuple((world.stratum_index, world.load_index) for world in worlds) != tuple(
        (stratum, load) for stratum in range(N_STRATA)
        for load in range(len(LOAD_GRID))
    ):
        raise ValueError("OD-5a run enumeration requires canonical stratum/load order")
    first_seed = worlds[0].design_seed
    per_stratum: dict[int, Mapping[str, float]] = {}
    for world in worlds:
        if world.design_seed != first_seed:
            raise ValueError("OD-5a design mixes unrelated Sobol seeds")
        if (set(world.nonload_coordinates) != NONLOAD_COORDINATES
                or any(isinstance(value, bool)
                       or not isinstance(value, (int, float)) or not isfinite(value)
                       for value in world.nonload_coordinates.values())):
            raise ValueError("OD-5a run matrix contains invalid non-load coordinates")
        if world.rho_nominal != LOAD_GRID[world.load_index]:
            raise ValueError("OD-5a load coordinate differs from preregistered grid")
        prior = per_stratum.setdefault(world.stratum_index, world.nonload_coordinates)
        if prior != world.nonload_coordinates:
            raise ValueError("OD-5a stratum changes non-load exogenous coordinates")
    for world in worlds:
        for arm in ARMS:
            for replication in range(N_REPLICATIONS):
                yield OD5aRunKey(
                    world_hash=world.world_hash,
                    crn_group=world.crn_group,
                    arm_id=arm,
                    replication_index=replication,
                )
