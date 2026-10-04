from sose.examples.hospitals.runtime import emergency_episode_id
from sose.examples.hospitals.simulation import (
    run_happy_path,
    run_icu_path,
    run_preemption_path,
)


def test_hospital_happy_path_discharges_without_live_capacity():
    persistence, entities = run_happy_path()

    assert persistence.entity(
        "hospital_admission", entities.admission_id
    ).state == "discharged"
    assert persistence.store_items() == ()
    assert persistence.store_put_intents() == ()
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()
    assert persistence.resource_release_intents() == ()


def test_hospital_icu_path_discharges_and_releases_capacity():
    persistence, entities = run_icu_path()

    assert persistence.entity(
        "hospital_admission", entities.admission_id
    ).state == "discharged"
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()
    assert persistence.resource_release_intents() == ()


def test_emergency_procedure_preemption_preserves_both_episode_histories():
    persistence, entities = run_preemption_path()

    assert persistence.entity(
        "hospital_admission", entities.admission_id
    ).state == "discharged"
    assert persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    ).state == "completed"

    emergency = persistence.entity(
        "hospital_treatment_episode",
        emergency_episode_id(entities.treatment_episode_id),
    )
    assert emergency is not None
    assert emergency.state == "completed"

    assert len(persistence.resource_preemption_results()) == 1
    assert persistence.preemptive_resource_demands() == ()
    assert persistence.preemptive_resource_reservations() == ()
    assert persistence.preemptive_resource_release_intents() == ()
