from datetime import datetime, timedelta, timezone

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.entity import Entity
from sose.probability import TransitionGraphBuilder, probabilistic


class Event:
    def __init__(self, event_id: str):
        self.id = event_id


class FakeChart:
    configuration_values = {"released"}

    def enabled_events(self, **kwargs):
        events = [Event("start"), Event("wait"), Event("cancel")]
        if kwargs.get("material_available") is False:
            return [event for event in events if event.id != "start"]
        return events


def build_context(seed: int = 42):
    return SimulationContext(
        clock=SimulationClock(
            datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
            timedelta(hours=1),
        ),
        random=RandomSource(seed),
        scheduler=Scheduler(),
    )


def build_graph():
    return (
        TransitionGraphBuilder("work_order")
        .edge("released", "start", "in_progress")
        .edge("released", "wait", "waiting_material")
        .edge("released", "cancel", "cancelled")
        .build()
    )


def test_runtime_decision_is_replayable_for_same_scope():
    ctx = build_context()
    entity = ctx.entities.create(Entity, entity_type="work_order", key=("wo-1",))
    policy = probabilistic({"start": 0.75, "wait": 0.20, "cancel": 0.05})

    first = ctx.transitions.decide(
        chart=FakeChart(),
        graph=build_graph(),
        policy=policy,
        entity=entity,
        context=ctx,
        scope=("normal-operation",),
    )
    second = ctx.transitions.decide(
        chart=FakeChart(),
        graph=build_graph(),
        policy=policy,
        entity=entity,
        context=ctx,
        scope=("normal-operation",),
    )

    assert first.event == second.event
    assert first.draw == second.draw
    assert first.probability == second.probability


def test_runtime_uses_statechart_enabled_events_as_guard_boundary():
    ctx = build_context()
    entity = ctx.entities.create(Entity, entity_type="work_order", key=("wo-1",))
    policy = probabilistic({"start": 1000, "wait": 1, "cancel": 1})

    decision = ctx.transitions.decide(
        chart=FakeChart(),
        graph=build_graph(),
        policy=policy,
        entity=entity,
        context=ctx,
        guard_kwargs={"material_available": False},
    )

    assert decision.event in {"wait", "cancel"}
    assert decision.event != "start"
