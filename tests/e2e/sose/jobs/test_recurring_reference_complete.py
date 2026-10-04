from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.examples.catalog import builtin_catalog
from sose.examples.credit_loans.simulation import installment_id, loan_id
from sose.examples.energy_utilities.simulation import dr_event_id
from sose.examples.order_to_cash.simulation import receivable_id
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


def _run(job: SimulationJob, count: int) -> None:
    for ordinal in range(1, count + 1):
        job.run_tick(trigger_id=f"{job.job_id}-{ordinal}")


def test_every_builtin_domain_supports_recurring_tick_reconciliation():
    missing = [
        definition.name
        for definition in builtin_catalog().definitions()
        if definition.reconcile_tick is None
    ]
    assert missing == []


def test_construction_recurring_job_advances_by_durable_boundaries():
    job = _job("construction", "construction-recurring")
    state = job.initialize({"planned_start_delay": timedelta(hours=1)})

    _run(job, 8)

    activity = job.persistence.entity(
        "construction_activity",
        state.bootstrap_state.activity_id,
    )
    assert activity is not None
    assert activity.state == "completed"


def test_manufacturing_recurring_job_completes_production_across_ticks():
    job = _job("manufacturing", "manufacturing-recurring")
    state = job.initialize({"quantity": 4.0})

    _run(job, 5)

    order = job.persistence.entity(
        "production_order",
        state.bootstrap_state.production_order_id,
    )
    assert order is not None
    assert order.state == "completed"


def test_p2p_recurring_job_waits_for_supplier_lead_time_then_consumes():
    job = _job("p2p", "p2p-recurring")
    state = job.initialize({"quantity": 3.0})

    _run(job, 12)

    demand = job.persistence.entity(
        "material_demand",
        state.bootstrap_state.material_demand_id,
    )
    assert demand is not None
    assert demand.state == "consumed"


def test_r2r_recurring_job_closes_period_after_configured_boundary():
    job = _job("record_to_report", "r2r-recurring")
    state = job.initialize({"close_delay": timedelta(hours=2)})

    _run(job, 6)

    period = job.persistence.entity(
        "accounting_period",
        state.bootstrap_state.period_id,
    )
    assert period is not None
    assert period.state == "closed"


def test_transit_recurring_job_reconciles_vehicle_across_both_trips():
    job = _job("transit", "transit-recurring")
    state = job.initialize(
        {
            "first_trip_delay": timedelta(hours=1),
            "trip_duration": timedelta(hours=1),
            "layover": timedelta(0),
        }
    )

    _run(job, 4)

    trip_a = job.persistence.entity(
        "transit_scheduled_trip",
        state.bootstrap_state.trip_a_id,
    )
    trip_b = job.persistence.entity(
        "transit_scheduled_trip",
        state.bootstrap_state.trip_b_id,
    )
    vehicle = job.persistence.entity(
        "transit_vehicle",
        state.bootstrap_state.vehicle_id,
    )
    assert trip_a is not None and trip_a.state == "completed"
    assert trip_b is not None and trip_b.state == "completed"
    assert vehicle is not None and vehicle.state == "available"
    assert vehicle.attributes["active_trip_id"] is None


def test_aviation_recurring_job_runs_rotation_without_end_to_end_wrapper():
    job = _job("aviation", "aviation-recurring")
    state = job.initialize(
        {
            "leg1_departure_delay": timedelta(hours=1),
            "leg2_departure_delay": timedelta(hours=5),
        }
    )

    _run(job, 9)

    leg1 = job.persistence.entity("aviation_flight", state.bootstrap_state.leg1_id)
    leg2 = job.persistence.entity("aviation_flight", state.bootstrap_state.leg2_id)
    assert leg1 is not None and leg1.state == "released"
    assert leg2 is not None and leg2.state == "released"


def test_credit_recurring_job_underwrites_services_and_pays_installments():
    job = _job("credit_loans", "credit-recurring")
    state = job.initialize(
        {
            "installment_count": 2,
            "tick_step": timedelta(days=1),
            "first_due_delay": timedelta(days=1),
            "installment_interval": timedelta(days=1),
        }
    )

    _run(job, 6)

    loan = job.persistence.entity(
        "loan",
        loan_id(state.bootstrap_state.application_id),
    )
    assert loan is not None
    installments = [
        job.persistence.entity(
            "loan_installment",
            installment_id(loan.id, ordinal),
        )
        for ordinal in (1, 2)
    ]
    assert all(item is not None and item.state == "paid" for item in installments)


def test_energy_recurring_job_emits_readings_and_reconciles_dr_boundaries():
    job = _job("energy_utilities", "energy-recurring")
    job.initialize(
        {
            "demand_response_start_delay": timedelta(hours=1),
            "demand_response_duration": timedelta(hours=1),
            "meter_reading_quantity": 7.5,
        }
    )

    _run(job, 4)

    event = job.persistence.entity("utility_dr_event", dr_event_id("dr-1"))
    assert event is not None
    assert event.state == "completed"
    readings = [
        event
        for event in job.persistence.events()
        if event.entity_type == "utility_meter_reading"
    ]
    assert readings


def test_insurance_recurring_job_reaches_paid_claim_through_tick_phases():
    job = _job("insurance", "insurance-recurring")
    state = job.initialize({"payment_delay": timedelta(hours=1)})

    _run(job, 7)

    claim = job.persistence.entity("insurance_claim", state.bootstrap_state.claim_id)
    assert claim is not None
    assert claim.state == "paid"


def test_o2c_recurring_job_collects_receivable_after_due_boundary():
    job = _job("order_to_cash", "o2c-recurring")
    state = job.initialize({"due_delay": timedelta(hours=1)})

    _run(job, 6)

    receivable = job.persistence.entity(
        "receivable",
        receivable_id(state.bootstrap_state.order_id),
    )
    assert receivable is not None
    assert receivable.state == "collected"
