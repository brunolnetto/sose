from sose.backends.simpy import SimPyBackend
from sose.examples.insurance.simulation import (
    ORIGIN,
    assessment_id,
    build_runtime,
    claim_next_for_assessment,
    complete_assessment,
    ensure_document_request,
    ensure_fraud_investigation,
    ensure_reserve,
    flow_correlation_id,
    fraud_investigation_id,
    queue_claim,
    reconcile_fraud,
    reserve_id,
    satisfy_documents,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def _ready_for_assessment():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    ensure_document_request(persistence, engine, backend, entities=entities)
    satisfy_documents(persistence, engine, entities=entities)
    queue_claim(persistence, engine, backend, entities=entities)
    assert claim_next_for_assessment(
        persistence,
        engine,
        backend,
        worker_id="crash",
    ) == entities.claim_id
    return persistence, entities, engine, backend


def test_satisfied_document_request_cancels_stale_deadline_after_crash():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    request = ensure_document_request(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    command = engine.context.commands.create(
        "satisfy",
        target=request,
        correlation_id=flow_correlation_id(entities.claim_id),
        key=("insurance-crash", request.id, "satisfy"),
    )
    engine.dispatch(command)

    assert persistence.entity(
        "insurance_document_request",
        request.id,
    ).state == "satisfied"
    assert len(persistence.scheduled_work()) == 1

    assert satisfy_documents(
        persistence,
        engine,
        entities=entities,
    )
    assert persistence.scheduled_work() == ()
    claim = persistence.entity("insurance_claim", entities.claim_id)
    assert claim is not None and claim.state == "ready_for_assessment"


def test_terminal_assessment_reconciles_claim_after_crash():
    persistence, entities, engine, backend = _ready_for_assessment()
    assessment = persistence.entity(
        "insurance_assessment",
        assessment_id(entities.claim_id),
    )
    assert assessment is not None and assessment.state == "in_progress"

    command = engine.context.commands.create(
        "approve",
        target=assessment,
        correlation_id=flow_correlation_id(entities.claim_id),
        key=("insurance-crash", assessment.id, "approve"),
    )
    engine.dispatch(command)

    claim = persistence.entity("insurance_claim", entities.claim_id)
    assert claim is not None and claim.state == "assessing"

    assert complete_assessment(
        persistence,
        engine,
        backend,
        entities=entities,
        worker_id="crash",
        outcome="approve",
    ) == "approved"
    claim = persistence.entity("insurance_claim", entities.claim_id)
    assert claim is not None and claim.state == "approved"
    assert persistence.resource_reservations() == ()


def test_terminal_fraud_investigation_reconciles_claim_after_crash():
    persistence, entities, engine, backend = _ready_for_assessment()
    complete_assessment(
        persistence,
        engine,
        backend,
        entities=entities,
        worker_id="crash",
        outcome="fraud",
    )
    investigation = ensure_fraud_investigation(
        persistence,
        engine,
        entities=entities,
    )
    request_id = f"fraud-investigator:{investigation.id}"
    engine.resources.request(
        backend,
        resource_name="fraud_investigator",
        request_id=request_id,
        requested_at=backend.now,
        priority=1,
    )
    backend.run_until(backend.now)

    investigation = persistence.entity(
        "insurance_fraud_investigation",
        fraud_investigation_id(entities.claim_id),
    )
    for event in ("assign", "clear"):
        command = engine.context.commands.create(
            event,
            target=investigation,
            correlation_id=flow_correlation_id(entities.claim_id),
            key=("insurance-crash-fraud", investigation.id, event),
        )
        engine.dispatch(command)
        investigation = persistence.entity(
            "insurance_fraud_investigation",
            investigation.id,
        )
        assert investigation is not None

    claim = persistence.entity("insurance_claim", entities.claim_id)
    assert investigation.state == "cleared"
    assert claim is not None and claim.state == "fraud_review"

    assert reconcile_fraud(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    claim = persistence.entity("insurance_claim", entities.claim_id)
    assert claim is not None and claim.state == "ready_for_assessment"
    assert persistence.resource_reservations() == ()


def test_proposed_reserve_is_established_on_retry():
    persistence, entities, engine, backend = _ready_for_assessment()
    complete_assessment(
        persistence,
        engine,
        backend,
        entities=entities,
        worker_id="crash",
        outcome="approve",
    )
    claim = persistence.entity("insurance_claim", entities.claim_id)
    reserve = engine.context.entities.create(
        __import__(
            "sose.examples.insurance.entities",
            fromlist=["Reserve"],
        ).Reserve,
        key=("insurance-reference", claim.id, "reserve"),
        state="proposed",
        attributes={"claim_id": claim.id, "amount": claim.attributes["amount"]},
    )
    with persistence.transaction() as uow:
        uow.save_entity(reserve)

    recovered = ensure_reserve(
        persistence,
        engine,
        entities=entities,
    )
    assert recovered.id == reserve_id(entities.claim_id)
    assert recovered.state == "established"


def test_catastrophe_invalidates_already_pending_adjuster_demand():
    from sose.examples.insurance.scenarios import catastrophe_capacity_scenario

    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence,
        scenarios=(catastrophe_capacity_scenario(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    ensure_document_request(persistence, engine, backend, entities=entities)
    satisfy_documents(persistence, engine, entities=entities)
    queue_claim(persistence, engine, backend, entities=entities)

    engine.resources.request(
        backend,
        resource_name="claims_adjuster",
        request_id="adjuster-blocker",
        requested_at=backend.now,
        priority=1,
    )
    backend.run_until(backend.now)
    assert claim_next_for_assessment(
        persistence,
        engine,
        backend,
        worker_id="pending-before-catastrophe",
    ) is None
    assert any(
        d.request_id == "claims-adjuster:pending-before-catastrophe"
        for d in persistence.resource_demands()
    )

    engine.advance_tick()
    backend.run_until(context.clock.now)
    assert context.scenarios.attribute(
        "insurance.adjuster.available",
        True,
    ) is False

    assert claim_next_for_assessment(
        persistence,
        engine,
        backend,
        worker_id="pending-before-catastrophe",
    ) is None
    assert not any(
        d.request_id == "claims-adjuster:pending-before-catastrophe"
        for d in persistence.resource_demands()
    )
