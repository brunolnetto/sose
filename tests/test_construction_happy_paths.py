from sose.examples.construction.runtime import inspection_id
from sose.examples.construction.simulation import run_happy_path, run_rework_path


def test_construction_happy_path_completes_without_live_capacity():
    persistence, entities = run_happy_path(quantity=10.0)

    assert persistence.entity(
        "construction_activity", entities.activity_id
    ).state == "completed"
    first = persistence.entity(
        "construction_inspection",
        inspection_id(entities.activity_id, 1),
    )
    assert first is not None
    assert first.state == "passed"
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()
    assert persistence.resource_release_intents() == ()
    assert persistence.store_items() == ()
    assert next(
        state.level
        for state in persistence.container_states()
        if state.name == "material_quantity"
    ) == 0.0


def test_failed_inspection_remains_durable_after_rework_passes():
    persistence, entities = run_rework_path(quantity=10.0)

    assert persistence.entity(
        "construction_activity", entities.activity_id
    ).state == "completed"
    first = persistence.entity(
        "construction_inspection",
        inspection_id(entities.activity_id, 1),
    )
    second = persistence.entity(
        "construction_inspection",
        inspection_id(entities.activity_id, 2),
    )
    assert first is not None and first.state == "failed"
    assert second is not None and second.state == "passed"
    assert first.id != second.id
    assert persistence.resource_reservations() == ()
