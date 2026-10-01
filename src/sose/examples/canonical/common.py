from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from statemachine import State, StateChart
from pydantic import Field

from sose.api import (
    DomainConfig, DomainRegistry, Engine, Entity, EntityType,
    RandomSource, Scheduler, SimulationClock, SimulationContext,
    probabilistic_transitions,
)

ORIGIN = datetime(2027, 1, 1, 9, tzinfo=timezone.utc)


@dataclass(slots=True)
class CanonicalCase(Entity):
    entity_type: str = "canonical_case"


@probabilistic_transitions({}, excluded_events={"advance", "finish"})
class CanonicalCaseChart(StateChart):
    ready = State(initial=True)
    active = State()
    completed = State(final=True)
    advance = ready.to(active)
    finish = active.to(completed)


class CanonicalConfig(DomainConfig):
    enabled: bool = True
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(minutes=1)
    random_seed: int = 1701
    participants: int = Field(default=3, ge=1)
    capacity: int = Field(default=1, ge=1)


def build_runtime(persistence, config: CanonicalConfig, now: datetime, tick: int):
    context = SimulationContext(
        clock=SimulationClock(now=now, step=config.tick_step, tick=tick),
        random=RandomSource(root_seed=config.random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("canonical_case", CanonicalCaseChart))
    return context, Engine(context=context, registry=registry, persistence=persistence)


def seed_case(persistence, *, name: str, attributes: dict[str, object]):
    context, _ = build_runtime(persistence, CanonicalConfig(), ORIGIN, 0)
    case = context.entities.create(
        CanonicalCase,
        key=("canonical", name),
        state="ready",
        attributes={"canonical": name, **attributes},
    )
    with persistence.transaction() as uow:
        uow.save_entity(case)
    return case


def transition(engine: Engine, case: CanonicalCase, event: str) -> None:
    command = engine.context.commands.create(
        event,
        target=case,
        correlation_id=case.id,
        key=("canonical", case.id, event),
    )
    engine.dispatch(command)
