from sose.examples.field_service.simulation import (
    REQUIRED_PART,
    run_happy_path,
)


def test_installation_uses_eligible_technician_part_and_visit_evidence():
    persistence, entities = run_happy_path()

    work_order = persistence.entity("field_work_order", entities.work_order_id)
    technicians = [
        persistence.entity("field_technician", technician_id)
        for technician_id in entities.technician_ids
    ]
    appointments = [
        persistence.entity("field_appointment", appointment_id)
        for appointment_id in work_order.attributes["appointment_ids"]
    ]

    assert work_order is not None and work_order.state == "completed"
    assert len(appointments) == 1
    appointment = appointments[0]
    assert appointment is not None and appointment.state == "completed"
    qualified = next(
        technician
        for technician in technicians
        if REQUIRED_PART is not None
        and "fiber-installation" in technician.attributes["skills"]
    )
    assert appointment.attributes["technician_id"] == qualified.id
    assert qualified.state == "available"

    selections = persistence.store_get_results()
    assert len(selections) == 1
    assert selections[0].item.value["sku"] == REQUIRED_PART
    assert persistence.resource_reservations() == ()
    assert persistence.scheduled_work() == ()
