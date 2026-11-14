"""Validated, immutable *definition* of composed organizational processes.

This is a configuration contract above the SOSE kernel (issue #495).
It does not instantiate StateCharts, persist snapshots, allocate resources,
impose a global clock, or confer business entity ownership on a supervisor.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal
import re
from hashlib import sha256
import json


def _identity(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a nonempty canonical identity")


def _positive(value: int, name: str, *, allow_zero: bool = False) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < (0 if allow_zero else 1)
    ):
        raise ValueError(f"{name} must be a {'nonnegative' if allow_zero else 'positive'} integer")


def _unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label}")


@dataclass(frozen=True, slots=True)
class ProcessComponent:
    """A process owns multiple StateChart *types*, not a replicated business state."""

    component_id: str
    domain: str
    owner_id: str
    statechart_types: tuple[str, ...]
    process_version: int = 1

    def __post_init__(self) -> None:
        for label in ("component_id", "domain", "owner_id"):
            _identity(getattr(self, label), label)
        _positive(self.process_version, "process_version")
        statecharts = tuple(self.statechart_types)
        if not statecharts:
            raise ValueError("process component must declare StateChart types")
        for name in statecharts:
            _identity(name, "statechart type")
        _unique(statecharts, "statechart type")
        object.__setattr__(self, "statechart_types", statecharts)


@dataclass(frozen=True, slots=True)
class BoundaryBinding:
    """A typed, versioned interaction between independently owned components."""

    producer_id: str
    consumer_id: str
    contract_name: str
    contract_version: int

    def __post_init__(self) -> None:
        for label in ("producer_id", "consumer_id", "contract_name"):
            _identity(getattr(self, label), label)
        _positive(self.contract_version, "contract_version")
        if self.producer_id == self.consumer_id:
            raise ValueError("cross-process boundary cannot consume its own ingress")


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """An independently triggered component job, not an organization-wide clock."""

    job_id: str
    component_id: str
    trigger_namespace: str

    def __post_init__(self) -> None:
        for label in ("job_id", "component_id", "trigger_namespace"):
            _identity(getattr(self, label), label)


@dataclass(frozen=True, slots=True)
class PhysicalPoolRef:
    """Reference to one external authoritative physical resource ledger."""

    pool_id: str
    owner_id: str
    consumer_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identity(self.pool_id, "pool_id")
        _identity(self.owner_id, "pool owner_id")
        consumers = tuple(self.consumer_ids)
        if not consumers:
            raise ValueError("physical pool requires at least one consumer")
        for name in consumers:
            _identity(name, "physical pool consumer")
        _unique(consumers, "physical pool consumer")
        object.__setattr__(self, "consumer_ids", consumers)


_CONFIG_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class OperationalConfigReference:
    """Provenance-bearing policy/calendar/cost reference, not execution authority.

    Merely supplying a content hash does NOT attest that a referenced policy,
    calendar, or cost model is installed or valid. Admission must fail closed
    until a trusted external registry and epoch migration are implemented.
    """

    component_id: str
    kind: Literal["policy", "calendar", "cost_model"]
    reference_id: str
    reference_version: int
    source_sha256: str
    config_epoch: int

    def __post_init__(self) -> None:
        _identity(self.component_id, "operational config component_id")
        _identity(self.reference_id, "operational config reference_id")
        if not isinstance(self.kind, str) or self.kind not in {"policy", "calendar", "cost_model"}:
            raise ValueError("unknown operational configuration kind")
        _positive(self.reference_version, "operational config reference_version")
        _positive(self.config_epoch, "operational config epoch", allow_zero=True)
        if not isinstance(self.source_sha256, str) or not _CONFIG_SHA256.fullmatch(self.source_sha256):
            raise ValueError("operational config requires canonical source SHA-256")


@dataclass(frozen=True, slots=True)
class OrganizationDefinition:
    """Catalog contract; constituent domains retain their own durable truth.

    No organization-level statechart, mutable aggregate entity, global
    simulation tick, or implicit correlation-ID-to-org-ID mapping is created.
    """

    organization_id: str
    version: int
    components: tuple[ProcessComponent, ...]
    boundaries: tuple[BoundaryBinding, ...] = field(default_factory=tuple)
    jobs: tuple[ScheduledJob, ...] = field(default_factory=tuple)
    physical_pools: tuple[PhysicalPoolRef, ...] = field(default_factory=tuple)
    config_epoch: int = 0
    operational_configs: tuple[OperationalConfigReference, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _identity(self.organization_id, "organization_id")
        _positive(self.version, "organization version")
        _positive(self.config_epoch, "config_epoch", allow_zero=True)

        components = tuple(self.components)
        boundaries = tuple(self.boundaries)
        jobs = tuple(self.jobs)
        pools = tuple(self.physical_pools)
        operational_configs = tuple(self.operational_configs)
        if not components:
            raise ValueError("organization must contain at least one process component")
        component_ids = tuple(component.component_id for component in components)
        _unique(component_ids, "component id")
        _unique(tuple((c.domain, c.owner_id) for c in components), "durable process owner")
        known = set(component_ids)
        _validate_boundaries(boundaries, known)
        _validate_jobs_and_pools(jobs, pools, known)
        _validate_operational_configs(operational_configs, known, self.config_epoch)

        object.__setattr__(self, "operational_configs", operational_configs)
        object.__setattr__(self, "components", components)
        object.__setattr__(self, "boundaries", boundaries)
        object.__setattr__(self, "jobs", jobs)
        object.__setattr__(self, "physical_pools", pools)

    def fingerprint(self) -> str:
        """Canonical configuration fingerprint, not an execution/commit digest."""
        # These fields encode sets of named components/edges, not execution
        # sequences. Canonicalize by identity rather than preserving caller
        # tuple order; changing physical/job/contract topology still changes
        # the digest while mere input reordering does not.
        payload = {
            "organization_id": self.organization_id,
            "version": self.version,
            "config_epoch": self.config_epoch,
            "components": sorted(
                (
                    {
                        **asdict(component),
                        "statechart_types": sorted(component.statechart_types),
                    }
                    for component in self.components
                ),
                key=lambda value: value["component_id"],
            ),
            "boundaries": sorted(
                (asdict(boundary) for boundary in self.boundaries),
                key=lambda value: (
                    value["producer_id"], value["consumer_id"],
                    value["contract_name"], value["contract_version"],
                ),
            ),
            "jobs": sorted(
                (asdict(job) for job in self.jobs),
                key=lambda value: value["job_id"],
            ),
            "physical_pools": sorted(
                (
                    {
                        **asdict(pool),
                        "consumer_ids": sorted(pool.consumer_ids),
                    }
                    for pool in self.physical_pools
                ),
                key=lambda value: value["pool_id"],
            ),
        }
        # Backward compatible: older v1 fingerprints did not have this
        # field. An empty set carries no additional semantic configuration;
        # including [] would silently invalidate durable admission hashes.
        if self.operational_configs:
            payload["operational_configs"] = sorted(
                (asdict(config) for config in self.operational_configs),
                key=lambda ref: (ref["component_id"], ref["kind"]),
            )
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
        return sha256(canonical.encode("utf-8")).hexdigest()


def _validate_boundaries(boundaries: tuple[BoundaryBinding, ...], known: set[str]) -> None:
    for boundary in boundaries:
        if boundary.producer_id not in known or boundary.consumer_id not in known:
            raise ValueError("boundary has dangling process component")
    _unique(tuple(
        (b.producer_id, b.consumer_id, b.contract_name, b.contract_version)
        for b in boundaries
    ), "causal boundary")


def _validate_jobs_and_pools(
    jobs: tuple[ScheduledJob, ...], pools: tuple[PhysicalPoolRef, ...], known: set[str],
) -> None:
    _unique(tuple(job.job_id for job in jobs), "job identity")
    _unique(tuple(job.trigger_namespace for job in jobs), "trigger namespace")
    if any(job.component_id not in known for job in jobs):
        raise ValueError("job has dangling process component")
    _unique(tuple(pool.pool_id for pool in pools), "physical pool reference")
    if any(name not in known for pool in pools for name in pool.consumer_ids):
        raise ValueError("physical pool has dangling consumer")


def _validate_operational_configs(
    operational_configs: tuple, known: set[str], config_epoch: int,
) -> None:
    for ref in operational_configs:
        if not isinstance(ref, OperationalConfigReference):
            raise TypeError("operational config requires OperationalConfigReference")
        if ref.component_id not in known:
            raise ValueError("operational config has dangling component")
        if ref.config_epoch != config_epoch:
            raise ValueError("operational config epoch contradicts organization config_epoch")
    _unique(tuple((ref.component_id, ref.kind) for ref in operational_configs),
            "operational config component/kind")


# Admission lives in organization_admission; keep its historical import path.
from .organization_admission import (  # noqa: E402,F401
    _reject_unadmitted_cycles,
    validate_organization_admission,
)
