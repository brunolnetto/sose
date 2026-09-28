from sose.examples.energy_utilities.scenarios import ORIGIN
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
        reading_id(entities.meter_id, ORIGIN),
    )
    assert reading is not None and reading.state == "committed"
    assert reading.attributes["quantity_kwh"] == 12.5

    event = persistence.entity("utility_dr_event", dr_event_id("dr-1"))
    assert event is not None and event.state == "completed"
    participation = persistence.entity(
        "utility_dr_participation",
        dr_participation_id(event.id, entities.service_point_id),
    )
    assert participation is not None and participation.state == "completed"
    assert persistence.scheduled_work() == ()
