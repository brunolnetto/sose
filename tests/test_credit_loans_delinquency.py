from sose.backends.simpy import SimPyBackend
from sose.examples.credit_loans.simulation import (
    COLLECTION_FOLLOWUP,
    ORIGIN,
    apply_restructure,
    build_runtime,
    collection_case_id,
    default_loan,
    delinquency_case_id,
    disburse_and_schedule,
    ensure_delinquency,
    installment_id,
    loan_id,
    reconcile_collection,
    reconcile_underwriting,
    schedule_overdue,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def _prepare_overdue():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    assert reconcile_underwriting(
        persistence, engine, backend, entities=entities, approve=True
    )
    due_dates = disburse_and_schedule(
        persistence, engine, backend, entities=entities
    )
    loan = persistence.entity("loan", loan_id(entities.application_id))
    iid = installment_id(loan.id, 1)
    backend.run_until(due_dates[0])
    overdue_at = schedule_overdue(
        persistence, engine, backend, installment_id_value=iid
    )
    backend.run_until(overdue_at)
    assert persistence.entity("loan_installment", iid).state == "overdue"
    return persistence, entities, engine, backend, loan.id, iid


def test_overdue_installment_enters_collection_and_can_default():
    persistence, entities, engine, backend, lid, iid = _prepare_overdue()
    delinquency = ensure_delinquency(
        persistence, engine, installment_id_value=iid
    )
    assert persistence.entity("loan", lid).state == "delinquent"

    assert reconcile_collection(
        persistence,
        engine,
        backend,
        installment_id_value=iid,
        promise=True,
    )
    collection = persistence.entity(
        "loan_collection_case", collection_case_id(delinquency.id)
    )
    assert collection is not None and collection.state == "promised"
    followup = engine.scheduler.find_pending(
        entity_type="loan_collection_case",
        entity_id=collection.id,
        name="escalate",
    )
    assert followup is not None
    backend.run_until(followup.work.due_at)
    collection = persistence.entity("loan_collection_case", collection.id)
    assert collection is not None and collection.state == "escalated"

    assert default_loan(persistence, engine, installment_id_value=iid)
    assert persistence.entity("loan", lid).state == "defaulted"
    assert persistence.entity(
        "delinquency_case",
        delinquency_case_id(lid, iid),
    ).state == "defaulted"


def test_restructure_preserves_old_delinquency_and_creates_new_obligation():
    persistence, entities, engine, backend, lid, iid = _prepare_overdue()
    delinquency = ensure_delinquency(
        persistence, engine, installment_id_value=iid
    )
    assert reconcile_collection(
        persistence, engine, backend, installment_id_value=iid
    )

    replacement = apply_restructure(
        persistence,
        engine,
        backend,
        installment_id_value=iid,
    )

    assert persistence.entity("loan_installment", iid).state == "restructured"
    assert persistence.entity("loan", lid).state == "servicing"
    assert persistence.entity(
        "delinquency_case", delinquency.id
    ).state == "restructured"
    assert replacement.state == "scheduled"
    assert replacement.attributes["amount"] == 1200.0
    assert engine.scheduler.find_pending(
        entity_type="loan_installment",
        entity_id=replacement.id,
        name="make_due",
    ) is not None
