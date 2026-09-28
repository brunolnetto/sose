from sose.examples.energy_utilities.simulation import (
    dr_event_id,
    dr_participation_id,
    reading_id,
    run_happy_path,
)


def test_energy_happy_path_commits_reading_and_completes_dr_event():
    persistence, entities = run_happy_path()

    reading = persistence.entity(
        "utility_meter_reading",
        reading_id(
            entities.meter_id,
            persistence.simulation_position().logical_time.replace(
                month=11,
                day=1,
                hour=8,
            ),
        ),
    )
    readings = [
        entity
        for (entity_type, _), entity in persistence._state.entities.items()
        if entity_type == "utility_meter_reading"
    ]
    assert len(readings) == 1
    assert readings[0].state == "committed"
    assert readings[0].attributes["quantity_kwh"] == 12.5

    event = persistence.entity("utility_dr_event", dr_event_id("dr-1"))
    assert event is not None and event.state == "completed"
    participation = persistence.entity(
        "utility_dr_participation",
        dr_participation_id(event.id, entities.service_point_id),
    )
    assert participation is not None and participation.state == "completed"
    assert persistence.scheduled_work() == ()
