from __future__ import annotations

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.hospitals.patient_flow import (
    claim_next_ward_admission,
    deteriorate_to_icu_wait,
    discharge_from_icu,
    discharge_from_ward,
    reconcile_icu_allocation,
    transfer_before_queue,
    transfer_from_icu_wait,
    triage_and_queue,
)
from sose.examples.hospitals.runtime import ORIGIN, admission, build_runtime, seed_reference
from sose.persistence.memory import MemoryPersistence


def _runtime(**seed_kwargs):
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, **seed_kwargs)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    return persistence, entities, engine, backend


def _set_state(persistence, admission_id: str, state: str) -> None:
    with persistence.transaction() as uow:
        current = uow.get_entity("hospital_admission", admission_id)
        assert current is not None
        current.state = state
        uow.save_entity(current)


def _block(engine, backend, resource_name: str, request_id: str) -> None:
    engine.resources.request(
        backend,
        resource_name=resource_name,
        request_id=request_id,
        requested_at=backend.now,
    )
    backend.run_until(backend.now)


def test_triage_and_transfer_reject_invalid_committed_states():
    persistence, entities, engine, backend = _runtime()
    _set_state(persistence, entities.admission_id, "transferred")

    with pytest.raises(RuntimeError, match="not waiting for ward bed"):
        triage_and_queue(
            persistence,
            engine,
            backend,
            admission_id=entities.admission_id,
        )

    with pytest.raises(RuntimeError, match="early transfer"):
        transfer_before_queue(
            persistence,
            engine,
            admission_id=entities.admission_id,
        )


@pytest.mark.parametrize(
    ("blocked_resource", "claim_id"),
    [
        ("ward_bed", "bed-blocked"),
        ("clinical_team", "team-blocked"),
    ],
)
def test_ward_claim_releases_partial_capacity_when_peer_resource_is_unavailable(
    blocked_resource, claim_id
):
    persistence, entities, engine, backend = _runtime()
    triage_and_queue(
        persistence,
        engine,
        backend,
        admission_id=entities.admission_id,
    )
    _block(engine, backend, blocked_resource, f"block:{blocked_resource}")

    assert claim_next_ward_admission(
        persistence,
        engine,
        backend,
        claim_id=claim_id,
    ) is None

    owned = {
        reservation.request_id
        for reservation in persistence.resource_reservations()
    }
    assert f"ward-bed:{claim_id}" not in owned
    assert f"ward-team:{claim_id}" not in owned


def test_ward_claim_with_empty_queue_releases_both_resources():
    persistence, _, engine, backend = _runtime()

    assert claim_next_ward_admission(
        persistence,
        engine,
        backend,
        claim_id="empty",
    ) is None

    assert not any(
        reservation.request_id in {"ward-bed:empty", "ward-team:empty"}
        for reservation in persistence.resource_reservations()
    )


def test_ward_discharge_and_deterioration_reject_wrong_states():
    persistence, entities, engine, backend = _runtime()

    with pytest.raises(RuntimeError, match="not discharge ready"):
        discharge_from_ward(
            persistence,
            engine,
            backend,
            admission_id=entities.admission_id,
            claim_id="unused",
        )

    with pytest.raises(RuntimeError, match="cannot deteriorate"):
        deteriorate_to_icu_wait(
            persistence,
            engine,
            backend,
            admission_id=entities.admission_id,
            ward_claim_id="unused",
        )


def test_reconcile_icu_short_circuits_terminal_and_non_waiting_states():
    persistence, entities, engine, backend = _runtime()

    assert reconcile_icu_allocation(
        persistence,
        engine,
        backend,
        admission_id=entities.admission_id,
    ) is False

    _set_state(persistence, entities.admission_id, "icu")
    assert reconcile_icu_allocation(
        persistence,
        engine,
        backend,
        admission_id=entities.admission_id,
    ) is True


@pytest.mark.parametrize("blocked_resource", ["icu_bed", "clinical_team"])
def test_reconcile_icu_releases_partial_capacity_on_contention(blocked_resource):
    persistence, entities, engine, backend = _runtime(acuity=1)
    _set_state(persistence, entities.admission_id, "waiting_icu")
    _block(engine, backend, blocked_resource, f"block:{blocked_resource}")

    assert reconcile_icu_allocation(
        persistence,
        engine,
        backend,
        admission_id=entities.admission_id,
    ) is False

    owned = {
        reservation.request_id
        for reservation in persistence.resource_reservations()
    }
    assert f"icu-bed:{entities.admission_id}" not in owned
    assert f"icu-team:{entities.admission_id}" not in owned


def test_icu_discharge_and_transfer_reject_wrong_states():
    persistence, entities, engine, backend = _runtime()

    with pytest.raises(RuntimeError, match="not discharge ready"):
        discharge_from_icu(
            persistence,
            engine,
            backend,
            admission_id=entities.admission_id,
        )

    with pytest.raises(RuntimeError, match="requires waiting_icu"):
        transfer_from_icu_wait(
            persistence,
            engine,
            backend,
            admission_id=entities.admission_id,
        )


def test_discharge_from_icu_accepts_already_discharge_ready_and_releases_capacity():
    persistence, entities, engine, backend = _runtime()
    _set_state(persistence, entities.admission_id, "discharge_ready")

    # No existing ICU reservations is itself a valid idempotent release path.
    discharge_from_icu(
        persistence,
        engine,
        backend,
        admission_id=entities.admission_id,
    )

    assert admission(persistence, entities.admission_id).state == "discharged"
