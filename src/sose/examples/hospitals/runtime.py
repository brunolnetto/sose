from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.runtime import (
    PreemptiveResourceDefinition,
    ResourceDefinition,
    StoreDefinition,
)
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import Admission, TreatmentEpisode
from .scenarios import ORIGIN
from .statecharts import AdmissionChart, TreatmentEpisodeChart


@dataclass(frozen=True, slots=True)
class HospitalEntities:
    admission_id: str
    treatment_episode_id: str


def flow_correlation_id(admission_id: str) -> str:
    return deterministic_id("hospital-flow", admission_id)


def emergency_episode_id(normal_episode_id: str) -> str:
    return deterministic_id(
        "entity",
        "hospital_treatment_episode",
        "hospital-reference",
        normal_episode_id,
        "emergency-1",
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 252,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("hospital_admission", AdmissionChart))
    registry.register(
        EntityType("hospital_treatment_episode", TreatmentEpisodeChart)
    )
    return context, Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    acuity: int = 50,
    service: str = "general-medicine",
    ward_bed_capacity: int = 1,
    icu_bed_capacity: int = 1,
    clinical_team_capacity: int = 1,
    procedure_suite_capacity: int = 1,
    triage_queue_capacity: int = 100,
) -> HospitalEntities:
    context, engine = build_runtime(persistence, now=now)
    admission = context.entities.create(
        Admission,
        key=("hospital-reference", "admission-1"),
        state="admitted",
        attributes={"acuity": acuity, "service": service},
    )
    episode = context.entities.create(
        TreatmentEpisode,
        key=("hospital-reference", "episode-1"),
        state="planned",
        attributes={
            "admission_id": admission.id,
            "kind": "elective-procedure",
            "emergency": False,
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(admission)
        uow.save_entity(episode)
        uow.save_resource_definition(ResourceDefinition("ward_bed", capacity=ward_bed_capacity))
        uow.save_resource_definition(ResourceDefinition("icu_bed", capacity=icu_bed_capacity))
        uow.save_resource_definition(ResourceDefinition("clinical_team", capacity=clinical_team_capacity))

    engine.preemptive_resources.define(
        PreemptiveResourceDefinition("procedure_suite", capacity=procedure_suite_capacity)
    )
    engine.stores.define(
        StoreDefinition("triage_queue", kind="priority", capacity=triage_queue_capacity)
    )
    return HospitalEntities(admission.id, episode.id)


def create_admission(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    key: str,
    acuity: int,
) -> Admission:
    admission = engine.context.entities.create(
        Admission,
        key=("hospital-reference", key),
        state="admitted",
        attributes={"acuity": acuity, "service": "general-medicine"},
    )
    with persistence.transaction() as uow:
        uow.save_entity(admission)
    return admission


def admission(persistence: MemoryPersistence, admission_id: str) -> Admission:
    value = persistence.entity("hospital_admission", admission_id)
    if value is None:
        raise RuntimeError(f"admission was not persisted: {admission_id}")
    return value


def episode(
    persistence: MemoryPersistence,
    episode_id: str,
) -> TreatmentEpisode:
    value = persistence.entity("hospital_treatment_episode", episode_id)
    if value is None:
        raise RuntimeError(f"treatment episode was not persisted: {episode_id}")
    return value


def maybe_episode(
    persistence: MemoryPersistence,
    episode_id: str,
) -> TreatmentEpisode | None:
    return persistence.entity("hospital_treatment_episode", episode_id)


def dispatch(
    engine: Engine,
    entity,
    event: str,
    *,
    key: tuple[object, ...],
    correlation_id: str,
) -> None:
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=correlation_id,
        key=key,
    )
    engine.dispatch(command)


