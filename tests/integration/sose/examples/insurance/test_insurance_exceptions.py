import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.insurance.simulation import (
    ORIGIN,
    assessment_id,
    build_runtime,
    claim_next_for_assessment,
    complete_assessment,
    ensure_document_request,
    ensure_fraud_investigation,
    ensure_payment,
    ensure_reserve,
    queue_claim,
    reconcile_fraud,
    reopen_claim,
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
    return persistence, entities, engine, backend


def test_expired_document_request_blocks_claim_progression():
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
    due_at = persistence.scheduled_work()[0].due_at
    backend.run_until(due_at)
    request = persistence.entity("insurance_document_request", request.id)
    claim = persistence.entity("insurance_claim", entities.claim_id)

    assert request is not None and request.state == "expired"
    assert claim is not None and claim.state == "pending_documents"
    with pytest.raises(RuntimeError, match="expired document request"):
        satisfy_documents(persistence, engine, entities=entities)


def test_confirmed_fraud_rejects_claim_without_reserve_or_payment():
    persistence, entities, engine, backend = _ready_for_assessment()
    assert claim_next_for_assessment(
        persistence, engine, backend, worker_id="fraud"
    ) == entities.claim_id
    assert complete_assessment(
        persistence,
        engine,
        backend,
        entities=entities,
        worker_id="fraud",
        outcome="fraud",
    ) == "fraud_flagged"

    investigation = ensure_fraud_investigation(
        persistence,
        engine,
        entities=entities,
    )
    assert investigation.state == "opened"
    assert reconcile_fraud(
        persistence,
        engine,
        backend,
        entities=entities,
        confirm=True,
    ) is False

    claim = persistence.entity("insurance_claim", entities.claim_id)
    assert claim is not None and claim.state == "rejected"
    with pytest.raises(RuntimeError, match="reserve requires approved"):
        ensure_reserve(persistence, engine, entities=entities)
    with pytest.raises(RuntimeError, match="payment requires"):
        ensure_payment(persistence, engine, entities=entities)


def test_cleared_fraud_requeues_and_requires_new_assessment():
    persistence, entities, engine, backend = _ready_for_assessment()
    claim_next_for_assessment(
        persistence, engine, backend, worker_id="fraud-clear"
    )
    complete_assessment(
        persistence,
        engine,
        backend,
        entities=entities,
        worker_id="fraud-clear",
        outcome="fraud",
    )
    assert reconcile_fraud(
        persistence,
        engine,
        backend,
        entities=entities,
        confirm=False,
    )

    claim = persistence.entity("insurance_claim", entities.claim_id)
    assert claim is not None and claim.state == "ready_for_assessment"
    queue_claim(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=2,
    )
    assert claim_next_for_assessment(
        persistence,
        engine,
        backend,
        worker_id="fraud-clear-2",
        assessment_ordinal=2,
    ) == entities.claim_id
    assert complete_assessment(
        persistence,
        engine,
        backend,
        entities=entities,
        worker_id="fraud-clear-2",
        outcome="approve",
        ordinal=2,
    ) == "approved"
    assert persistence.entity(
        "insurance_assessment",
        assessment_id(entities.claim_id, 1),
    ).state == "fraud_flagged"
    assert persistence.entity(
        "insurance_assessment",
        assessment_id(entities.claim_id, 2),
    ).state == "approved"


def test_rejected_claim_can_reopen_with_new_document_request():
    persistence, entities, engine, backend = _ready_for_assessment()
    claim_next_for_assessment(
        persistence, engine, backend, worker_id="reject"
    )
    complete_assessment(
        persistence,
        engine,
        backend,
        entities=entities,
        worker_id="reject",
        outcome="reject",
    )
    reopened = reopen_claim(persistence, engine, entities=entities)
    assert reopened.state == "reopened"

    request = ensure_document_request(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=2,
    )
    claim = persistence.entity("insurance_claim", entities.claim_id)
    assert request.state == "open"
    assert claim is not None and claim.state == "pending_documents"
