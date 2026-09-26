import pytest

from sose.examples.mro.simulation import build_runtime, seed_reference
from sose.persistence.memory import MemoryPersistence
from sose.probability.graph import NoProbabilisticTransition


@pytest.mark.parametrize(
    ("state", "blocked_event"),
    [
        ("released", "start"),
        ("waiting_material", "material_ready"),
        ("waiting_resource", "resource_ready"),
        ("in_progress", "interrupt"),
        ("interrupted", "resume"),
    ],
)
def test_mro_reconciler_only_events_are_not_probabilistically_dispatchable(
    state,
    blocked_event,
):
    persistence = MemoryPersistence()
    ids = seed_reference(persistence)
    _, engine = build_runtime(persistence)

    work_order = persistence.entity("work_order", ids.work_order_id)
    work_order.state = state
    with persistence.transaction() as uow:
        uow.save_entity(work_order)

    current = persistence.entity("work_order", ids.work_order_id)
    policy = engine.context.statecharts.policy_for(current)

    assert policy.is_probabilistically_eligible(blocked_event) is False


def test_released_work_order_probabilistic_choice_cannot_select_start():
    persistence = MemoryPersistence()
    ids = seed_reference(persistence)
    _, engine = build_runtime(persistence)

    work_order = persistence.entity("work_order", ids.work_order_id)
    work_order.state = "released"
    with persistence.transaction() as uow:
        uow.save_entity(work_order)

    current = persistence.entity("work_order", ids.work_order_id)
    decision = engine.choose_transition(
        current,
        scope=("mro-direct-probabilistic",),
    )

    assert decision.event != "start"
