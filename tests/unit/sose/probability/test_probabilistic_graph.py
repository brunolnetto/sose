from datetime import datetime, timedelta, timezone

import pytest

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.entity import Entity
from sose.probability import (
    NoProbabilisticTransition,
    StateConfiguration,
    TransitionGraphBuilder,
    probabilistic,
)


def context(seed: int = 42) -> SimulationContext:
    return SimulationContext(
        clock=SimulationClock(
            now=datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
            step=timedelta(hours=1),
        ),
        random=RandomSource(seed),
        scheduler=Scheduler(),
    )


def order(ctx: SimulationContext) -> Entity:
    return ctx.entities.create(Entity, entity_type="order", key=("order-1",), state="released")


def graph():
    return (
        TransitionGraphBuilder("order")
        .edge("released", "start", "in_progress")
        .edge("released", "wait", "waiting_material")
        .edge("released", "cancel", "cancelled")
        .build()
    )


def test_guard_filtering_happens_before_probability_normalization():
    ctx = context()
    policy = probabilistic({"start": 0.75, "wait": 0.20, "cancel": 0.05})

    distribution = graph().distribution(
        configuration=StateConfiguration(("released",)),
        policy=policy,
        entity=order(ctx),
        context=ctx,
        enabled_events={"wait", "cancel"},
    )

    assert [option.event for option in distribution] == ["cancel", "wait"]
    probabilities = {option.event: option.probability for option in distribution}
    assert probabilities["wait"] == pytest.approx(0.8)
    assert probabilities["cancel"] == pytest.approx(0.2)


def test_contextual_weight_can_depend_on_entity_and_simulation_context():
    ctx = context()
    entity = order(ctx)
    entity.attributes["material_ready"] = False
    policy = probabilistic(
        {
            "start": lambda ev: 8.0 if ev.entity.attributes["material_ready"] else 0.1,
            "wait": lambda ev: 0.1 if ev.entity.attributes["material_ready"] else 8.0,
            "cancel": 0.1,
        }
    )

    distribution = graph().distribution(
        configuration=StateConfiguration(("released",)),
        policy=policy,
        entity=entity,
        context=ctx,
        enabled_events={"start", "wait", "cancel"},
    )

    probabilities = {option.event: option.probability for option in distribution}
    assert probabilities["wait"] > 0.97
    assert probabilities["start"] < 0.02


def test_zero_weight_events_are_not_sampled():
    ctx = context()
    distribution = graph().distribution(
        configuration=StateConfiguration(("released",)),
        policy=probabilistic({"start": 0.0, "wait": 1.0, "cancel": 0.0}),
        entity=order(ctx),
        context=ctx,
        enabled_events={"start", "wait", "cancel"},
    )

    assert [option.event for option in distribution] == ["wait"]
    assert distribution[0].probability == 1.0


def test_no_positive_weight_transition_fails_explicitly():
    ctx = context()
    with pytest.raises(NoProbabilisticTransition):
        graph().distribution(
            configuration=StateConfiguration(("released",)),
            policy=probabilistic({"start": 0, "wait": 0, "cancel": 0}),
            entity=order(ctx),
            context=ctx,
            enabled_events={"start", "wait", "cancel"},
        )


def test_parent_state_edge_is_available_in_hierarchical_configuration():
    ctx = context()
    entity = order(ctx)
    hierarchical = (
        TransitionGraphBuilder("hierarchical")
        .edge("fulfillment", "cancel", "cancelled")
        .build()
    )

    distribution = hierarchical.distribution(
        configuration=StateConfiguration(("fulfillment", "picking")),
        policy=probabilistic({"cancel": 1.0}),
        entity=entity,
        context=ctx,
        enabled_events={"cancel"},
    )

    assert len(distribution) == 1
    assert distribution[0].event == "cancel"


def test_parallel_edges_for_same_event_are_one_probabilistic_choice():
    ctx = context()
    entity = order(ctx)
    parallel = (
        TransitionGraphBuilder("parallel")
        .edge("payment_pending", "abort", "payment_cancelled")
        .edge("shipment_pending", "abort", "shipment_cancelled")
        .edge("shipment_pending", "ship", "shipped")
        .build()
    )

    distribution = parallel.distribution(
        configuration=StateConfiguration(("payment_pending", "shipment_pending")),
        policy=probabilistic({"abort": 0.25, "ship": 0.75}),
        entity=entity,
        context=ctx,
        enabled_events={"abort", "ship"},
    )

    options = {option.event: option for option in distribution}
    assert options["abort"].probability == pytest.approx(0.25)
    assert len(options["abort"].edges) == 2
    assert options["ship"].probability == pytest.approx(0.75)
