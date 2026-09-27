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
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(hours=1), tick=tick),
        random=RandomSource(root_seed=252),
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
    acuity: int = 50,
) -> HospitalEntities:
    context, engine = build_runtime(persistence)
    admission = context.entities.create(
        Admission,
        key=("hospital-reference", "admission-1"),
        state="admitted",
        attributes={"acuity": acuity, "service": "general-medicine"},
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
        uow.save_resource_definition(ResourceDefinition("ward_bed", capacity=1))
        uow.save_resource_definition(ResourceDefinition("icu_bed", capacity=1))
        uow.save_resource_definition(ResourceDefinition("clinical_team", capacity=1))

    engine.preemptive_resources.define(
        PreemptiveResourceDefinition("procedure_suite", capacity=1)
    )
    engine.stores.define(
        StoreDefinition("triage_queue", kind="priority", capacity=100)
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


def preemptive_request_exists(
    persistence: MemoryPersistence,
    request_id: str,
) -> bool:
    return any(
        demand.request_id == request_id
        for demand in persistence.preemptive_resource_demands()
    ) or any(
        reservation.request_id == request_id
        for reservation in persistence.preemptive_resource_reservations()
    )


def preemptive_reservation(persistence: MemoryPersistence, request_id: str):
    return next(
        (
            reservation
            for reservation in persistence.preemptive_resource_reservations()
            if reservation.request_id == request_id
        ),
        None,
    )
