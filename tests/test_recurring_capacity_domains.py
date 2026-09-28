from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.examples.catalog import builtin_catalog
from sose.jobs.runner import SimulationJob
from sose.persistence.memory import MemoryPersistence


def _backend(origin):
    return SimPyBackend(origin=origin)


def _job(domain: str, job_id: str) -> SimulationJob:
    return SimulationJob(
        job_id=job_id,
        definition=builtin_catalog().get(domain),
        persistence=MemoryPersistence(),
        backend_factory=_backend,
    )


def test_itsm_recurring_job_uses_configured_capacity_and_closes_incident():
    job = _job("itsm", "itsm-recurring")
    state = job.initialize(
        {
            "service": "identity-api",
            "support_agent_capacity": 2,
            "escalation_manager_capacity": 3,
            "incident_queue_capacity": 25,
            "sla_delay": timedelta(hours=6),
        }
    )

    incident = job.persistence.entity("itsm_incident", state.bootstrap_state.incident_id)
    assert incident.attributes["service"] == "identity-api"
    resources = {item.name: item.capacity for item in job.persistence.resource_definitions()}
    assert resources["support_agent"] == 2
    assert resources["escalation_manager"] == 3
    assert job.persistence.store_definitions()[0].capacity == 25

    result = job.run_tick(trigger_id="itsm-1")
    incident = job.persistence.entity("itsm_incident", state.bootstrap_state.incident_id)

    assert result.logical_tick == 1
    assert incident.state == "closed"


def test_hospital_recurring_job_uses_capacity_config_and_discharges():
    job = _job("hospitals", "hospital-recurring")
    state = job.initialize(
        {
            "service": "cardiology",
            "ward_bed_capacity": 3,
            "icu_bed_capacity": 2,
            "clinical_team_capacity": 4,
            "procedure_suite_capacity": 2,
            "triage_queue_capacity": 50,
        }
    )

    admission = job.persistence.entity(
        "hospital_admission",
        state.bootstrap_state.admission_id,
    )
    assert admission.attributes["service"] == "cardiology"
    resources = {item.name: item.capacity for item in job.persistence.resource_definitions()}
    assert resources["ward_bed"] == 3
    assert resources["icu_bed"] == 2
    assert resources["clinical_team"] == 4
    assert job.persistence.preemptive_resource_definitions()[0].capacity == 2
    assert job.persistence.store_definitions()[0].capacity == 50

    job.run_tick(trigger_id="hospital-1")
    admission = job.persistence.entity(
        "hospital_admission",
        state.bootstrap_state.admission_id,
    )
    assert admission.state == "discharged"


def test_field_service_recurring_job_respects_appointment_boundary():
    job = _job("field_service", "field-recurring")
    state = job.initialize(
        {
            "appointment_start_delay": timedelta(hours=1),
            "appointment_duration": timedelta(hours=1),
            "auto_seed_required_part": True,
        }
    )

    first = job.run_tick(trigger_id="field-1")
    work_order = job.persistence.entity(
        "field_work_order",
        state.bootstrap_state.work_order_id,
    )
    assert first.logical_tick == 1
    assert work_order.state == "scheduled"
    active_id = work_order.attributes["active_appointment_id"]
    appointment = job.persistence.entity("field_appointment", active_id)
    assert appointment.state == "confirmed"

    second = job.run_tick(trigger_id="field-2")
    work_order = job.persistence.entity(
        "field_work_order",
        state.bootstrap_state.work_order_id,
    )
    assert second.logical_tick == 2
    assert work_order.state == "completed"


def test_hospitality_recurring_job_follows_arrival_and_departure_boundaries():
    job = _job("hospitality", "hospitality-recurring")
    state = job.initialize(
        {
            "arrival_after": timedelta(hours=2),
            "stay_duration": timedelta(hours=1),
            "hold_duration": timedelta(hours=4),
            "no_show_grace": timedelta(hours=2),
        }
    )

    first = job.run_tick(trigger_id="hospitality-1")
    assert first.logical_tick == 1

    hotel = job.persistence.entity("hospitality_hotel", state.bootstrap_state.hotel_id)
    assert len(hotel.attributes["booking_ids"]) == 1

    second = job.run_tick(trigger_id="hospitality-2")
    reservation_id = builtin_catalog().get("hospitality").name
    reservations = [
        entity
        for entity in job.persistence._state.entities.values()
        if entity.entity_type == "hospitality_reservation"
    ]
    assert len(reservations) == 1
    assert second.logical_tick == 2
    assert reservations[0].state == "checked_in"

    third = job.run_tick(trigger_id="hospitality-3")
    reservation = job.persistence.entity(
        "hospitality_reservation",
        reservations[0].id,
    )
    assert third.logical_tick == 3
    assert reservation.state == "checked_out"


def test_airport_recurring_job_waits_for_arrival_and_departure_slot():
    job = _job("airports", "airport-recurring")
    state = job.initialize(
        {
            "arrival_delay": timedelta(hours=1),
            "departure_slot_delay": timedelta(hours=3),
            "gate_capacity": 2,
            "ground_team_capacity": 2,
            "tug_capacity": 2,
            "departure_queue_capacity": 20,
            "gate": "G9",
        }
    )

    first = job.run_tick(trigger_id="airport-1")
    turnaround = job.persistence.entity(
        "airport_flight_turnaround",
        state.bootstrap_state.turnaround_id,
    )
    gate = job.persistence.entity(
        "airport_gate_assignment",
        state.bootstrap_state.gate_assignment_id,
    )
    assert first.logical_tick == 1
    assert turnaround.state == "scheduled"
    assert gate.attributes["gate"] == "G9"

    second = job.run_tick(trigger_id="airport-2")
    turnaround = job.persistence.entity(
        "airport_flight_turnaround",
        state.bootstrap_state.turnaround_id,
    )
    assert second.logical_tick == 2
    assert turnaround.state == "waiting_slot"

    third = job.run_tick(trigger_id="airport-3")
    turnaround = job.persistence.entity(
        "airport_flight_turnaround",
        state.bootstrap_state.turnaround_id,
    )
    assert third.logical_tick == 3
    assert turnaround.state == "waiting_slot"

    fourth = job.run_tick(trigger_id="airport-4")
    turnaround = job.persistence.entity(
        "airport_flight_turnaround",
        state.bootstrap_state.turnaround_id,
    )
    assert fourth.logical_tick == 4
    assert turnaround.state == "departed"
