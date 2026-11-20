"""#631: runtimes draw through ``draw_uniform`` so each RNG model keeps its contract."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.randomness import CounterRandomSource, RandomSource, draw_uniform
from sose.core.scheduler import Scheduler
from sose.domain.entity import Entity
from sose.probability import TransitionGraphBuilder, probabilistic


def test_legacy_source_keeps_its_historical_first_draw() -> None:
    legacy = RandomSource(42)
    draw = draw_uniform(
        legacy, stream="s", entity_id="e", mechanism="m", legacy_scope=("a", 1)
    )
    assert draw == RandomSource(42).for_scope("a", 1).random()


def test_counter_source_draws_by_logical_identity() -> None:
    counter = CounterRandomSource(42)
    draw = draw_uniform(
        counter,
        stream="s",
        entity_id="e",
        mechanism="m",
        draw_index=3,
        legacy_scope=("ignored",),
    )
    assert draw == counter.uniform(stream="s", entity_id="e", mechanism="m", draw_index=3)


class _Event:
    def __init__(self, event_id: str) -> None:
        self.id = event_id


class _Chart:
    configuration_values = {"released"}

    def enabled_events(self, **_kwargs):
        return [_Event("start"), _Event("wait")]


def _decide(random_source, *, tick: int = 0):
    ctx = SimulationContext(
        clock=SimulationClock(
            datetime(2026, 1, 1, tzinfo=timezone.utc), timedelta(hours=1), tick=tick
        ),
        random=random_source,
        scheduler=Scheduler(),
    )
    entity = ctx.entities.create(Entity, entity_type="work_order", key=("wo-1",))
    graph = (
        TransitionGraphBuilder("work_order")
        .edge("released", "start", "in_progress")
        .edge("released", "wait", "waiting")
        .build()
    )
    return entity, ctx.transitions.decide(
        chart=_Chart(),
        graph=graph,
        policy=probabilistic({"start": 0.5, "wait": 0.5}),
        entity=entity,
        context=ctx,
        scope=("lane",),
    )


def test_transition_runtime_uses_counter_identity() -> None:
    counter = CounterRandomSource(7)
    entity, decision = _decide(counter, tick=2)
    assert decision.draw == counter.uniform(
        stream="transition",
        entity_id=("work_order", entity.id, ("released",), ("lane",)),
        mechanism="statechart-transition",
        draw_index=2,
    )
    assert _decide(CounterRandomSource(7), tick=2)[1] == decision


def test_transition_runtime_keeps_legacy_stream() -> None:
    entity, decision = _decide(RandomSource(7), tick=2)
    expected = RandomSource(7).for_scope(
        "transition", 2, "work_order", entity.id, "released", "lane"
    ).random()
    assert decision.draw == expected


def _tick_decision(random_source, *, tick: int):
    from sose.scenarios.engine import ScenarioEngine
    from sose.scenarios.model import Scenario, TickTrigger, TransitionWeightEffect

    engine = ScenarioEngine(
        random_source=random_source,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        tick=lambda: tick,
        context=lambda: None,
    )
    engine.register(Scenario(
        "surge", TickTrigger(), (TransitionWeightEffect(event="approve", multiplier=2.0),),
        activation_probability=0.5,
    ))
    (decision,) = engine.on_tick()
    return decision


def test_scenario_engine_uses_counter_identity() -> None:
    counter = CounterRandomSource(11)
    decision = _tick_decision(counter, tick=4)
    assert decision.draw == counter.uniform(
        stream="scenario-activation", entity_id=("tick", 4), mechanism="surge",
    )
    assert _tick_decision(CounterRandomSource(11), tick=4).draw == decision.draw


def test_scenario_engine_keeps_legacy_stream() -> None:
    decision = _tick_decision(RandomSource(11), tick=4)
    expected = RandomSource(11).for_scope("scenario-activation", "surge", "tick", 4).random()
    assert decision.draw == expected
