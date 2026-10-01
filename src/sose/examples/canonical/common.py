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
from sose.core.runtime import StoreDefinition
from sose.core.stores import DurableStoreManager

ORIGIN = datetime(2027, 1, 1, 9, tzinfo=timezone.utc)
ACTION_STORE = "__canonical_actions__"


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
    stores = DurableStoreManager(persistence)
    if not any(definition.name == ACTION_STORE for definition in persistence.store_definitions()):
        stores.define(StoreDefinition(ACTION_STORE))
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



def resolve_tick_action(
    persistence,
    backend,
    *,
    canonical: str,
    logical_tick: int,
    candidate: str,
    requested_at: datetime,
) -> str:
    """Durably bind one semantic action to a logical tick before side effects."""
    item_id = f"{canonical}:tick:{logical_tick}"
    existing = next(
        (
            item
            for item in persistence.store_items()
            if item.store_name == ACTION_STORE and item.item_id == item_id
        ),
        None,
    )
    if existing is not None:
        return str(existing.value["action"])

    pending = next(
        (
            intent
            for intent in persistence.store_put_intents()
            if intent.store_name == ACTION_STORE and intent.item_id == item_id
        ),
        None,
    )
    if pending is not None:
        return str(pending.value["action"])

    value = {"action": candidate}
    stores = DurableStoreManager(persistence)
    stores.ensure_put(
        backend,
        store_name=ACTION_STORE,
        item_id=item_id,
        value=value,
        requested_at=requested_at,
    )
    return candidate

def transition(engine: Engine, case: CanonicalCase, event: str) -> None:
    command = engine.context.commands.create(
        event,
        target=case,
        correlation_id=case.id,
        key=("canonical", case.id, event),
    )
    engine.dispatch(command)
