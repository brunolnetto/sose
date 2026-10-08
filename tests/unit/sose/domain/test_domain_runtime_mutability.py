from sose.backends.simpy import SimPyBackend
from sose.examples.catalog import builtin_catalog
from sose.jobs.runner import SimulationJob
from sose.persistence.memory import MemoryPersistence


BASE_RUNTIME_FIELDS = {"tick_step", "random_seed"}


def _backend(origin):
    return SimPyBackend(origin=origin)


def test_every_builtin_domain_exposes_domain_specific_runtime_control():
    for definition in builtin_catalog().definitions():
        domain_specific = set(definition.runtime_mutable_fields) - BASE_RUNTIME_FIELDS
        assert domain_specific, definition.name
        assert "start_at" not in definition.runtime_mutable_fields


def test_runtime_controls_are_exposed_by_parameter_discovery():
    for definition in builtin_catalog().definitions():
        described = {
            item["field_name"]: item["mutability"]
            for item in definition.describe_config()["parameters"]
        }
        for field_name in definition.runtime_mutable_fields:
            assert described[field_name] == "runtime"


def test_warehouse_progression_can_be_enabled_between_ticks():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("warehouse_fulfillment")
    job = SimulationJob(
        job_id="warehouse-runtime-control",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )
    state = job.initialize({"auto_progress_fulfillment": False})

    job.run_tick(trigger_id="warehouse-1")
    order = persistence.entity(
        "warehouse_fulfillment_order",
        state.bootstrap_state.order_id,
    )
    assert order is not None
    assert order.state == "requested"

    updated = job.update_config({"auto_progress_fulfillment": True})
    assert updated.config_revision == 2

    job.run_tick(trigger_id="warehouse-2")
    order = persistence.entity(
        "warehouse_fulfillment_order",
        state.bootstrap_state.order_id,
    )
    assert order is not None
    assert order.state == "picking"

    for index in range(3, 7):
        job.run_tick(trigger_id=f"warehouse-{index}")

    order = persistence.entity(
        "warehouse_fulfillment_order",
        state.bootstrap_state.order_id,
    )
    assert order is not None
    assert order.state == "shipped"


def test_hospital_progression_can_be_paused_and_resumed_by_config():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("hospitals")
    job = SimulationJob(
        job_id="hospital-runtime-control",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )
    state = job.initialize({"auto_progress_patient_flow": False})

    job.run_tick(trigger_id="hospital-1")
    admission = persistence.entity(
        "hospital_admission",
        state.bootstrap_state.admission_id,
    )
    assert admission is not None
    assert admission.state == "admitted"

    job.update_config({"auto_progress_patient_flow": True})
    job.run_tick(trigger_id="hospital-2")

    admission = persistence.entity(
        "hospital_admission",
        state.bootstrap_state.admission_id,
    )
    assert admission is not None
    assert admission.state == "discharged"
