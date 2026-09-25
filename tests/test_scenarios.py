from datetime import datetime, timedelta, timezone

import pytest

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.entity import Entity
from sose.domain.registry import DomainRegistry
from sose.persistence.memory import MemoryPersistence
from sose.probability import StateConfiguration, TransitionGraphBuilder, probabilistic
from sose.scenarios import (
    AttributeEffect,
    CompositeEffect,
    EventTrigger,
    Scenario,
    ScheduledTrigger,
    TickTrigger,
    TransitionWeightEffect,
)

NOW = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def context(seed: int = 42) -> SimulationContext:
    return SimulationContext(
        clock=SimulationClock(NOW, timedelta(hours=1)),
        random=RandomSource(seed),
        scheduler=Scheduler(),
    )


def test_tick_scenario_activates_environment_effect_without_mutating_entity():
    ctx = context()
    entity = ctx.entities.create(
        Entity, entity_type="supplier", key=("s1",), attributes={"available": True}
    )
    ctx.scenarios.register(
        Scenario(
            name="supplier_disruption",
            trigger=TickTrigger(),
            effects=(AttributeEffect("supplier.s1.available", False),),
        )
    )

    decisions = ctx.scenarios.on_tick()

    assert len(decisions) == 1
    assert decisions[0].activated is True
    assert ctx.scenarios.attribute("supplier.s1.available") is False
    assert entity.attributes["available"] is True


def test_condition_false_prevents_activation_without_consuming_probability_draw():
    ctx = context()
    ctx.scenarios.register(
        Scenario(
            name="never",
            trigger=TickTrigger(),
            condition=lambda evaluation: False,
            effects=(AttributeEffect("flag", True),),
        )
    )

    decision = ctx.scenarios.on_tick()[0]

    assert decision.activated is False
    assert decision.draw is None
    assert decision.reason == "condition_false"
    assert ctx.scenarios.attribute("flag", None) is None


def test_activation_probability_is_deterministic_for_same_seed_and_signal():
    def run(seed: int):
        ctx = context(seed)
        ctx.scenarios.register(
            Scenario(
                name="shock",
                trigger=TickTrigger(),
                activation_probability=0.37,
                effects=(AttributeEffect("shock", True),),
            )
        )
        return ctx.scenarios.on_tick()[0]

    first = run(99)
    second = run(99)

    assert first.attempt_id == second.attempt_id
    assert first.draw == second.draw
    assert first.activated == second.activated


def test_same_trigger_occurrence_is_idempotent():
    ctx = context()
    ctx.scenarios.register(
        Scenario(
            name="one_attempt_per_tick",
            trigger=TickTrigger(),
            effects=(AttributeEffect("active", True),),
        )
    )

    first = ctx.scenarios.on_tick()[0]
    second = ctx.scenarios.on_tick()[0]

    assert first == second
    assert len(ctx.scenarios.active_activations) == 1


def test_event_trigger_filters_and_preserves_causal_metadata():
    ctx = context()
    order = ctx.entities.create(Entity, entity_type="order", key=("o1",))
    event = ctx.events.create("order.paid", entity=order, key=("paid", order.id))
    ctx.scenarios.register(
        Scenario(
            name="fraud_campaign_reaction",
            trigger=EventTrigger(event="order.paid", entity_type="order"),
            effects=(AttributeEffect("fraud.pressure", "high"),),
        )
    )

    decision = ctx.scenarios.on_event(event)[0]
    activation = ctx.scenarios.active_activations[0]

    assert decision.activated is True
    assert activation.causation_id == event.event_id
    assert activation.correlation_id == event.correlation_id


def test_scheduled_scenario_activates_once_when_due():
    ctx = context()
    due = NOW + timedelta(hours=2)
    ctx.scenarios.register(
        Scenario(
            name="planned_outage",
            trigger=ScheduledTrigger(due),
            effects=(AttributeEffect("plant.available", False),),
        )
    )

    assert ctx.scenarios.on_tick() == ()
    ctx.clock.advance(2)
    first = ctx.scenarios.on_tick()
    second = ctx.scenarios.on_tick()

    assert len(first) == 1 and first[0].activated
    assert second == first
    assert len(ctx.scenarios.active_activations) == 1


def test_temporary_effect_expires_and_reveals_previous_environment_value():
    ctx = context()
    ctx.scenarios.register(
        Scenario(
            name="temporary_outage",
            trigger=TickTrigger(every=100),
            duration=timedelta(hours=2),
            effects=(AttributeEffect("plant.available", False),),
        )
    )

    ctx.scenarios.on_tick()
    assert ctx.scenarios.attribute("plant.available", True) is False

    ctx.clock.advance(2)
    ctx.scenarios.expire_due()

    assert ctx.scenarios.attribute("plant.available", True) is True
    assert ctx.scenarios.active_activations == ()


def test_composite_effect_applies_environment_and_transition_modifier():
    ctx = context()
    ctx.scenarios.register(
        Scenario(
            name="shortage",
            trigger=TickTrigger(),
            effects=(
                CompositeEffect(
                    (
                        AttributeEffect("inventory.shortage", True),
                        TransitionWeightEffect("wait", multiplier=4.0, entity_type="work_order"),
                    )
                ),
            ),
        )
    )
    ctx.scenarios.on_tick()

    assert ctx.scenarios.attribute("inventory.shortage") is True
    assert ctx.scenarios.transition_weight_multiplier("work_order", "wait") == 4.0


def test_active_transition_weight_effect_changes_probability_distribution():
    ctx = context()
    entity = ctx.entities.create(Entity, entity_type="work_order", key=("wo1",), state="released")
    graph = (
        TransitionGraphBuilder("work_order")
        .edge("released", "start", "in_progress")
        .edge("released", "wait", "waiting_material")
        .build()
    )
    ctx.scenarios.register(
        Scenario(
            name="material_shortage",
            trigger=TickTrigger(),
            effects=(TransitionWeightEffect("wait", multiplier=4.0, entity_type="work_order"),),
        )
    )
    ctx.scenarios.on_tick()

    distribution = graph.distribution(
        configuration=StateConfiguration(("released",)),
        policy=probabilistic({"start": 1.0, "wait": 1.0}),
        entity=entity,
        context=ctx,
        enabled_events={"start", "wait"},
        weight_transform=ctx.scenarios.transform_transition_weight,
    )

    probabilities = {option.event: option.probability for option in distribution}
    assert probabilities == pytest.approx({"start": 0.2, "wait": 0.8})


def test_zero_transition_multiplier_can_disable_branch():
    ctx = context()
    ctx.scenarios.register(
        Scenario(
            name="hard_block",
            trigger=TickTrigger(),
            effects=(TransitionWeightEffect("start", multiplier=0.0, entity_type="work_order"),),
        )
    )
    ctx.scenarios.on_tick()

    assert ctx.scenarios.transition_weight_multiplier("work_order", "start") == 0.0


def test_duplicate_scenario_name_is_rejected():
    ctx = context()
    scenario = Scenario(
        name="duplicate",
        trigger=TickTrigger(),
        effects=(AttributeEffect("x", 1),),
    )
    ctx.scenarios.register(scenario)

    with pytest.raises(ValueError, match="already registered"):
        ctx.scenarios.register(scenario)


def test_engine_evaluates_tick_scenarios_before_advancing_clock():
    ctx = context()
    scenario = Scenario(
        name="shift_start",
        trigger=TickTrigger(),
        effects=(AttributeEffect("shift", "day"),),
    )
    engine = Engine(
        context=ctx,
        registry=DomainRegistry(),
        persistence=MemoryPersistence(),
        scenarios=(scenario,),
    )

    engine.advance_tick()

    assert ctx.scenarios.attribute("shift") == "day"
    assert ctx.clock.tick == 1


def test_invalid_transition_multiplier_is_rejected():
    with pytest.raises(ValueError):
        TransitionWeightEffect("start", multiplier=-1.0)


def test_invalid_scenario_probability_is_rejected():
    with pytest.raises(ValueError):
        Scenario(
            name="invalid",
            trigger=TickTrigger(),
            activation_probability=1.1,
            effects=(AttributeEffect("x", 1),),
        )


class _Event:
    def __init__(self, event_id: str):
        self.id = event_id


class _ReleasedChart:
    configuration_values = {"released"}

    def enabled_events(self, **kwargs):
        return [_Event("start"), _Event("wait")]


def test_probabilistic_runtime_applies_active_scenario_modifiers_automatically():
    ctx = context()
    entity = ctx.entities.create(Entity, entity_type="work_order", key=("wo2",), state="released")
    graph = (
        TransitionGraphBuilder("work_order")
        .edge("released", "start", "in_progress")
        .edge("released", "wait", "waiting_material")
        .build()
    )
    ctx.scenarios.register(
        Scenario(
            name="block_start",
            trigger=TickTrigger(),
            effects=(TransitionWeightEffect("start", multiplier=0.0, entity_type="work_order"),),
        )
    )
    ctx.scenarios.on_tick()

    decision = ctx.transitions.decide(
        chart=_ReleasedChart(),
        graph=graph,
        policy=probabilistic({"start": 1.0, "wait": 1.0}),
        entity=entity,
        context=ctx,
    )

    assert decision.event == "wait"
    assert decision.probability == 1.0


def test_non_reentrant_scenario_does_not_stack_effects_while_active():
    ctx = context()
    ctx.scenarios.register(
        Scenario(
            name="disruption",
            trigger=TickTrigger(),
            effects=(TransitionWeightEffect("wait", multiplier=2.0, entity_type="work_order"),),
        )
    )

    first = ctx.scenarios.on_tick()[0]
    ctx.clock.advance()
    second = ctx.scenarios.on_tick()[0]

    assert first.activated is True
    assert second.activated is False
    assert second.reason == "already_active"
    assert ctx.scenarios.transition_weight_multiplier("work_order", "wait") == 2.0



def test_active_activations_excludes_expired_effects_without_explicit_expire_call():
    ctx = context()
    ctx.scenarios.register(
        Scenario(
            name="temporary_overlay",
            trigger=TickTrigger(every=100),
            duration=timedelta(hours=1),
            effects=(AttributeEffect("plant.available", False),),
        )
    )

    ctx.scenarios.on_tick()
    assert len(ctx.scenarios.active_activations) == 1

    ctx.clock.advance()

    assert ctx.scenarios.active_activations == ()


def test_effect_readers_ignore_expired_activations_without_new_signal():
    ctx = context()
    ctx.scenarios.register(
        Scenario(
            name="temporary_shortage",
            trigger=TickTrigger(every=100),
            duration=timedelta(hours=1),
            effects=(
                AttributeEffect("inventory.shortage", True),
                TransitionWeightEffect("wait", multiplier=4.0, entity_type="work_order"),
            ),
        )
    )

    ctx.scenarios.on_tick()
    assert ctx.scenarios.attribute("inventory.shortage", False) is True
    assert ctx.scenarios.transition_weight_multiplier("work_order", "wait") == 4.0

    ctx.clock.advance()

    assert ctx.scenarios.attribute("inventory.shortage", False) is False
    assert ctx.scenarios.transition_weight_multiplier("work_order", "wait") == 1.0
