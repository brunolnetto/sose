from __future__ import annotations

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.credit_loans import simulation as credit
from sose.persistence.memory import MemoryPersistence


def _prepare_overdue():
    persistence = MemoryPersistence()
    entities = credit.seed_reference(persistence)
    _, engine = credit.build_runtime(persistence)
    backend = SimPyBackend(origin=credit.ORIGIN)
    engine.rebuild_backend(backend)

    assert credit.reconcile_underwriting(
        persistence,
        engine,
        backend,
        entities=entities,
        approve=True,
    )
    due_dates = credit.disburse_and_schedule(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    loan = persistence.entity("loan", credit.loan_id(entities.application_id))
    assert loan is not None
    installment_id = credit.installment_id(loan.id, 1)
    backend.run_until(due_dates[0])
    overdue_at = credit.schedule_overdue(
        persistence,
        engine,
        backend,
        installment_id_value=installment_id,
    )
    backend.run_until(overdue_at)
    assert persistence.entity("loan_installment", installment_id).state == "overdue"
    return persistence, entities, engine, backend, loan.id, installment_id


def _prepare_servicing():
    persistence = MemoryPersistence()
    entities = credit.seed_reference(persistence)
    _, engine = credit.build_runtime(persistence)
    backend = SimPyBackend(origin=credit.ORIGIN)
    engine.rebuild_backend(backend)
    assert credit.reconcile_underwriting(
        persistence,
        engine,
        backend,
        entities=entities,
        approve=True,
    )
    credit.disburse_and_schedule(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    loan = persistence.entity("loan", credit.loan_id(entities.application_id))
    assert loan is not None and loan.state == "servicing"
    return persistence, entities, engine, backend, loan


def test_delinquency_recovery_persists_missing_case_already_referenced_by_loan():
    persistence, _, engine, _, loan_id, installment_id = _prepare_overdue()
    loan = persistence.entity("loan", loan_id)
    assert loan is not None
    did = credit.delinquency_case_id(loan.id, installment_id)

    # Simulate a crash/recovery state in which the loan already records the
    # deterministic delinquency id but the delinquency entity was not committed.
    loan.attributes["delinquency_case_ids"] = [did]
    with persistence.transaction() as uow:
        uow.save_entity(loan)

    assert persistence.entity("delinquency_case", did) is None

    case = credit.ensure_delinquency(
        persistence,
        engine,
        installment_id_value=installment_id,
    )

    assert case.id == did
    assert persistence.entity("delinquency_case", did) is not None
    assert persistence.entity("loan", loan_id).state == "delinquent"


def test_collection_unavailable_withdraws_any_request_and_returns_false(monkeypatch):
    persistence, _, engine, backend, _, installment_id = _prepare_overdue()
    delinquency = credit.ensure_delinquency(
        persistence,
        engine,
        installment_id_value=installment_id,
    )
    collection_id = credit.collection_case_id(delinquency.id)
    request_id = f"collection-agent:{collection_id}"

    engine.resources.request(
        backend,
        resource_name="collection_agent",
        request_id=request_id,
        requested_at=backend.now,
        priority=1,
    )
    backend.run_until(backend.now)
    assert engine.resources.reservation_for(request_id) is not None

    original_attribute = engine.context.scenarios.attribute

    def unavailable(key, default=None):
        if key == "credit_loans.collection.available":
            return False
        return original_attribute(key, default)

    monkeypatch.setattr(engine.context.scenarios, "attribute", unavailable)

    assert credit.reconcile_collection(
        persistence,
        engine,
        backend,
        installment_id_value=installment_id,
    ) is False

    assert engine.resources.reservation_for(request_id) is None
    assert all(
        demand.request_id != request_id
        for demand in persistence.resource_demands()
    )


def test_collection_returns_pending_when_agent_capacity_is_busy():
    persistence, _, engine, backend, _, installment_id = _prepare_overdue()

    engine.resources.request(
        backend,
        resource_name="collection_agent",
        request_id="collection-blocker",
        requested_at=backend.now,
        priority=1,
    )
    backend.run_until(backend.now)

    assert credit.reconcile_collection(
        persistence,
        engine,
        backend,
        installment_id_value=installment_id,
    ) is False

    installment = persistence.entity("loan_installment", installment_id)
    assert installment is not None
    request_id = f"collection-agent:{credit.collection_case_id(credit.delinquency_case_id(installment.attributes['loan_id'], installment_id))}"
    assert any(
        demand.request_id == request_id
        for demand in persistence.resource_demands()
    )


def test_cure_requires_paid_installment():
    persistence, _, engine, _, _, installment_id = _prepare_overdue()
    credit.ensure_delinquency(
        persistence,
        engine,
        installment_id_value=installment_id,
    )

    with pytest.raises(RuntimeError, match="cure requires paid installment"):
        credit.cure_delinquency(
            persistence,
            engine,
            installment_id_value=installment_id,
        )


def test_cure_resolves_collection_and_cancels_followup():
    persistence, _, engine, backend, loan_id, installment_id = _prepare_overdue()
    delinquency = credit.ensure_delinquency(
        persistence,
        engine,
        installment_id_value=installment_id,
    )
    assert credit.reconcile_collection(
        persistence,
        engine,
        backend,
        installment_id_value=installment_id,
        promise=True,
    )

    collection_id = credit.collection_case_id(delinquency.id)
    collection = persistence.entity("loan_collection_case", collection_id)
    assert collection is not None and collection.state == "promised"
    assert engine.scheduler.find_pending(
        entity_type="loan_collection_case",
        entity_id=collection_id,
        name="escalate",
    ) is not None

    installment = persistence.entity("loan_installment", installment_id)
    assert installment is not None
    remaining = round(
        float(installment.attributes["amount"])
        - float(installment.attributes["paid_amount"]),
        2,
    )
    credit.post_payment(
        persistence,
        engine,
        installment_id_value=installment_id,
        payment_ordinal=1,
        amount=remaining,
    )
    assert persistence.entity("loan_installment", installment_id).state == "paid"

    assert credit.cure_delinquency(
        persistence,
        engine,
        installment_id_value=installment_id,
    )

    assert persistence.entity("delinquency_case", delinquency.id).state == "cured"
    assert persistence.entity("loan_collection_case", collection_id).state == "resolved"
    assert engine.scheduler.find_pending(
        entity_type="loan_collection_case",
        entity_id=collection_id,
        name="escalate",
    ) is None
    assert persistence.entity("loan", loan_id).state == "servicing"


def test_restructure_rejects_servicing_loan_without_existing_restructure():
    persistence, _, engine, backend, loan = _prepare_servicing()
    installment_id = credit.installment_id(loan.id, 1)

    with pytest.raises(
        RuntimeError,
        match="restructure requires delinquent loan or existing restructure",
    ):
        credit.apply_restructure(
            persistence,
            engine,
            backend,
            installment_id_value=installment_id,
        )


def test_default_requires_delinquency_to_be_in_collection():
    persistence, _, engine, _, _, installment_id = _prepare_overdue()
    credit.ensure_delinquency(
        persistence,
        engine,
        installment_id_value=installment_id,
    )

    with pytest.raises(RuntimeError, match="default requires active collection"):
        credit.default_loan(
            persistence,
            engine,
            installment_id_value=installment_id,
        )


def test_default_requires_collection_to_be_escalated():
    persistence, _, engine, backend, _, installment_id = _prepare_overdue()
    credit.ensure_delinquency(
        persistence,
        engine,
        installment_id_value=installment_id,
    )
    assert credit.reconcile_collection(
        persistence,
        engine,
        backend,
        installment_id_value=installment_id,
        promise=False,
    )

    delinquency = persistence.entity(
        "delinquency_case",
        credit.delinquency_case_id(
            persistence.entity("loan_installment", installment_id).attributes["loan_id"],
            installment_id,
        ),
    )
    assert delinquency is not None and delinquency.state == "collection"
    collection = persistence.entity(
        "loan_collection_case",
        credit.collection_case_id(delinquency.id),
    )
    assert collection is not None and collection.state == "contacted"

    with pytest.raises(RuntimeError, match="default requires escalated collection"):
        credit.default_loan(
            persistence,
            engine,
            installment_id_value=installment_id,
        )
