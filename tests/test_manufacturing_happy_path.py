from datetime import timedelta

import pytest

from sose.examples.manufacturing.simulation import (
    ORIGIN,
    build_runtime,
    flow_correlation_id,
    run_happy_path,
    seed_happy_path,
)
from sose.persistence.memory import MemoryPersistence


def test_manufacturing_happy_path_reaches_completed_output():
    persistence, ids = run_happy_path(quantity=10.0)

    assert persistence.entity("production_order", ids.production_order_id).state == "completed"
    assert persistence.entity("manufacturing_operation", ids.operation_id).state == "done"
    assert persistence.store_items()[0].item_id == "wip-1"
    assert {s.name: s.level for s in persistence.container_states()} == {
        "finished_goods": 10.0,
        "raw_material": 0.0,
    }
    assert persistence.scheduled_work() == ()
    assert {e.correlation_id for e in persistence.events()} == {flow_correlation_id()}


def test_release_and_readiness_are_durable_scheduled_work():
    persistence = MemoryPersistence()
    ids = seed_happy_path(persistence, quantity=5.0)

    work = persistence.scheduled_work()
    assert len(work) == 2
    release = next(
        persistence.command(item.command_id)
        for item in work
        if persistence.command(item.command_id).name == "release"
    )
    assert release.entity_id == ids.production_order_id
    assert release.due_at == ORIGIN + timedelta(hours=1)


def test_quantity_must_fit_capacities():
    with pytest.raises(ValueError, match="capacity"):
        run_happy_path(quantity=1001.0)
