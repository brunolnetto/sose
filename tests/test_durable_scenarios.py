from datetime import datetime, timedelta, timezone

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.examples.mro.entities import WorkOrder
from sose.examples.mro.statecharts import WorkOrderChart
from sose.persistence.memory import MemoryPersistence
from sose.scenarios import AttributeEffect, EventTrigger, Scenario, TickTrigger, TransitionWeightEffect


ORIGIN = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def build(store, *, now=ORIGIN, tick=0, scenarios=()):
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(hours=1), tick=tick),
        random=RandomSource(42),
        scheduler=Scheduler(),
    )
    engine = Engine(
        context=context,
        registry=DomainRegistry(),
        persistence=store,
        scenarios=scenarios,
    )
    return context, engine


def test_rebuild_restores_active_scenario_effects_and_decisions():
    scenario = Scenario(
        name="temporary_disruption",
        trigger=TickTrigger(),
        duration=timedelta(hours=4),
        effects=(
            AttributeEffect("plant.available", False),
            TransitionWeightEffect("wait", 3.0, entity_type="work_order"),
        ),
    )
    store = MemoryPersistence()
    first_context, first_engine = build(store, scenarios=(scenario,))

    first_engine.advance_tick()

    saved = store.scenario_state()
    assert saved is not None
    assert len(saved.activations) == 1
    assert len(saved.decisions) == 1
    assert first_context.scenarios.attribute("plant.available", True) is False
    assert first_context.scenarios.transition_weight_multiplier("work_order", "wait") == 3.0

    position = store.simulation_position()
    second_context, second_engine = build(
        store,
        now=position.logical_time,
        tick=0,
        scenarios=(scenario,),
    )

    class EmptyBackend:
        @property
        def now(self):
            return position.logical_time

        def schedule_at(self, at, callback, *, priority=100, key=None):
            raise AssertionError("no scheduled work expected")

    assert second_engine.rebuild_backend(EmptyBackend()) == 0

    assert second_context.clock.tick == position.logical_tick
    assert second_context.scenarios.attribute("plant.available", True) is False
    assert second_context.scenarios.transition_weight_multiplier("work_order", "wait") == 3.0
    assert second_context.scenarios.decisions == first_context.scenarios.decisions
    assert second_context.scenarios.active_activations == first_context.scenarios.active_activations


def test_restored_decision_prevents_duplicate_activation_for_same_tick():
    scenario = Scenario(
        name="one_attempt",
        trigger=TickTrigger(every=10),
        duration=timedelta(hours=5),
        effects=(AttributeEffect("flag", True),),
    )
    store = MemoryPersistence()
    _, first_engine = build(store, scenarios=(scenario,))
    first_engine.advance_tick()

    position = store.simulation_position()
    second_context, second_engine = build(
        store,
        now=position.logical_time,
        tick=0,
        scenarios=(scenario,),
    )

    class EmptyBackend:
        @property
        def now(self):
            return position.logical_time

        def schedule_at(self, at, callback, *, priority=100, key=None):
            raise AssertionError("no scheduled work expected")

    second_engine.rebuild_backend(EmptyBackend())

    # Rewind only the signal coordinates to reproduce the already-evaluated occurrence.
    second_context.clock.now = ORIGIN
    second_context.clock.tick = 0
    decisions = second_context.scenarios.on_tick()

    assert len(decisions) == 1
    assert decisions[0] == store.scenario_state().decisions[0]
    assert len(second_context.scenarios.active_activations) == 1


def test_event_triggered_scenario_state_is_durable_across_restart():
    scenario = Scenario(
        name="release_pressure",
        trigger=EventTrigger(event="work_order.released", entity_type="work_order"),
        duration=timedelta(hours=3),
        effects=(AttributeEffect("release.pressure", "high"),),
    )
    store = MemoryPersistence()
    context = SimulationContext(
        clock=SimulationClock(now=ORIGIN, step=timedelta(hours=1)),
        random=RandomSource(42),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("work_order", WorkOrderChart))
    work_order = context.entities.create(
        WorkOrder,
        key=("durable-scenario-event", 1),
        state="planned",
    )
    with store.transaction() as uow:
        uow.save_entity(work_order)

    engine = Engine(
        context=context,
        registry=registry,
        persistence=store,
        scenarios=(scenario,),
    )
    command = context.commands.create(
        "release",
        target=work_order,
        key=("durable-scenario-event", work_order.id, "release"),
    )

    engine.dispatch(command)

    assert context.scenarios.attribute("release.pressure") == "high"
    saved = store.scenario_state()
    assert saved is not None
    assert len(saved.activations) == 1
    assert len(saved.decisions) == 1

    restarted_context = SimulationContext(
        clock=SimulationClock(now=ORIGIN, step=timedelta(hours=1)),
        random=RandomSource(42),
        scheduler=Scheduler(),
    )
    restarted_engine = Engine(
        context=restarted_context,
        registry=registry,
        persistence=store,
        scenarios=(scenario,),
    )

    class EmptyBackend:
        @property
        def now(self):
            position = store.simulation_position()
            return position.logical_time if position is not None else ORIGIN

        def schedule_at(self, at, callback, *, priority=100, key=None):
            raise AssertionError("no scheduled work expected")

    restarted_engine.rebuild_backend(EmptyBackend())

    assert restarted_context.scenarios.attribute("release.pressure") == "high"
    assert restarted_context.scenarios.decisions == context.scenarios.decisions
