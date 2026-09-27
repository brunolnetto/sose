from sose.backends.simpy import SimPyBackend
from sose.examples.insurance.scenarios import (
    catastrophe_capacity_scenario,
    payment_processor_outage_scenario,
)
from sose.examples.insurance.simulation import (
    ORIGIN,
    build_runtime,
    claim_next_for_assessment,
    complete_assessment,
    ensure_document_request,
    ensure_reserve,
    queue_claim,
    reconcile_payment,
    satisfy_documents,
    schedule_payment,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_catastrophe_capacity_strain_defers_adjuster_and_recovers():
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

    engine.advance_tick()
    backend.run_until(context.clock.now)
    assert context.scenarios.attribute(
        "insurance.adjuster.available", True
    ) is False

    assert claim_next_for_assessment(
        persistence,
        engine,
        backend,
        worker_id="catastrophe",
    ) is None
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()

    for _ in range(5):
        engine.advance_tick()
    backend.run_until(context.clock.now)

    assert claim_next_for_assessment(
        persistence,
        engine,
        backend,
        worker_id="catastrophe",
    ) == entities.claim_id


def test_payment_outage_preserves_due_payment_and_recovers():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence,
        scenarios=(payment_processor_outage_scenario(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    ensure_document_request(persistence, engine, backend, entities=entities)
    satisfy_documents(persistence, engine, entities=entities)
    queue_claim(persistence, engine, backend, entities=entities)
    claim_next_for_assessment(
        persistence,
        engine,
        backend,
        worker_id="payment-outage",
    )
    complete_assessment(
        persistence,
        engine,
        backend,
        entities=entities,
        worker_id="payment-outage",
        outcome="approve",
    )
    ensure_reserve(persistence, engine, entities=entities)

    due_at = schedule_payment(
        persistence,
        engine,
        backend,
        entities=entities,
        delay=context.clock.step,
    )

    engine.advance_tick()
    backend.run_until(due_at)
    assert context.scenarios.attribute(
        "insurance.payment.available", True
    ) is False

    assert reconcile_payment(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    payment = persistence.entity(
        "insurance_payment",
        __import__(
            "sose.examples.insurance.simulation",
            fromlist=["payment_id"],
        ).payment_id(entities.claim_id),
    )
    assert payment is not None and payment.state == "due"
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()

    for _ in range(3):
        engine.advance_tick()
    backend.run_until(context.clock.now)

    assert reconcile_payment(
        persistence,
        engine,
        backend,
        entities=entities,
    )
