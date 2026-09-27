from __future__ import annotations

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .runtime import (
    admission,
    dispatch,
    flow_correlation_id,
    resource_request_exists,
    resource_reservation,
)


def triage_and_queue(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    admission_id: str,
) -> None:
    current = admission(persistence, admission_id)
    correlation_id = flow_correlation_id(current.id)

    if current.state == "admitted":
        dispatch(
            engine,
            current,
            "triage",
            key=("hospital", current.id, "triage"),
            correlation_id=correlation_id,
        )
        current = admission(persistence, current.id)

    if current.state == "triaged":
        dispatch(
            engine,
            current,
            "wait_bed",
            key=("hospital", current.id, "wait-bed"),
            correlation_id=correlation_id,
        )
        current = admission(persistence, current.id)

    if current.state != "waiting_bed":
        raise RuntimeError(
            f"admission is not waiting for ward bed: {current.state}"
        )

    item_id = f"triage:{current.id}"
    queued = any(item.item_id == item_id for item in persistence.store_items())
    pending = any(
        intent.item_id == item_id for intent in persistence.store_put_intents()
    )
    consumed = any(
        result.item.item_id == item_id
        for result in persistence.store_get_results()
    )
    if not (queued or pending or consumed):
        engine.stores.put(
            backend,
            store_name="triage_queue",
            item_id=item_id,
            value={"admission_id": current.id},
            priority=int(current.attributes["acuity"]),
            requested_at=backend.now,
        )
        backend.run_until(backend.now)


def transfer_before_queue(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    admission_id: str,
) -> None:
    current = admission(persistence, admission_id)
    if current.state == "admitted":
        dispatch(
            engine,
            current,
            "triage",
            key=("hospital", current.id, "triage"),
            correlation_id=flow_correlation_id(current.id),
        )
        current = admission(persistence, current.id)

    if current.state != "triaged":
        raise RuntimeError(
            "early transfer is legal only before ward queue commitment"
        )

    dispatch(
        engine,
        current,
        "transfer",
        key=("hospital", current.id, "early-transfer"),
        correlation_id=flow_correlation_id(current.id),
    )


def claim_next_ward_admission(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    claim_id: str,
) -> str | None:
    if not engine.context.scenarios.attribute("hospital.ward.available", True):
        return None

    bed_request = f"ward-bed:{claim_id}"
    team_request = f"ward-team:{claim_id}"
    bed = engine.resources.ensure_requested(
        backend,
        resource_name="ward_bed",
        request_id=bed_request,
        requested_at=backend.now,
    )
    team = engine.resources.ensure_requested(
        backend,
        resource_name="clinical_team",
        request_id=team_request,
        requested_at=backend.now,
    )
    if bed is None or team is None:
        if bed is not None:
            engine.resources.withdraw(backend, bed_request)
        if team is not None:
            engine.resources.withdraw(backend, team_request)
        return None

    get_id = f"ward-claim:{claim_id}"
    result = engine.stores.ensure_selection(
        backend,
        store_name="triage_queue",
        request_id=get_id,
        requested_at=backend.now,
    )

    if result is None:
        engine.resources.withdraw(backend, bed_request)
        engine.resources.withdraw(backend, team_request)
        return None

    admission_id = str(result.item.value["admission_id"])
    current = admission(persistence, admission_id)
    if current.state == "waiting_bed":
        dispatch(
            engine,
            current,
            "allocate_bed",
            key=("hospital", current.id, "allocate-bed"),
            correlation_id=flow_correlation_id(current.id),
        )
        current = admission(persistence, current.id)
    if current.state == "bed_allocated":
        dispatch(
            engine,
            current,
            "start_treatment",
            key=("hospital", current.id, "start-treatment"),
            correlation_id=flow_correlation_id(current.id),
        )
    return admission_id


def release_ward_capacity(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    claim_id: str,
) -> None:
    for request_id in (f"ward-bed:{claim_id}", f"ward-team:{claim_id}"):
        engine.resources.withdraw(backend, request_id)


def discharge_from_ward(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    admission_id: str,
    claim_id: str,
) -> None:
    current = admission(persistence, admission_id)
    if current.state == "treatment":
        dispatch(
            engine,
            current,
            "ready_discharge",
            key=("hospital", current.id, "ready-discharge"),
            correlation_id=flow_correlation_id(current.id),
        )
        current = admission(persistence, current.id)

    if current.state != "discharge_ready":
        raise RuntimeError(
            f"admission is not discharge ready: {current.state}"
        )

    dispatch(
        engine,
        current,
        "discharge",
        key=("hospital", current.id, "discharge"),
        correlation_id=flow_correlation_id(current.id),
    )
    release_ward_capacity(
        persistence,
        engine,
        backend,
        claim_id=claim_id,
    )


def deteriorate_to_icu_wait(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    admission_id: str,
    ward_claim_id: str,
) -> None:
    current = admission(persistence, admission_id)
    if current.state not in {"bed_allocated", "treatment"}:
        raise RuntimeError(
            f"admission cannot deteriorate from {current.state}"
        )

    dispatch(
        engine,
        current,
        "deteriorate",
        key=("hospital", current.id, "deteriorate"),
        correlation_id=flow_correlation_id(current.id),
    )
    release_ward_capacity(
        persistence,
        engine,
        backend,
        claim_id=ward_claim_id,
    )


def reconcile_icu_allocation(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    admission_id: str,
) -> bool:
    current = admission(persistence, admission_id)
    if current.state == "icu":
        return True
    if current.state != "waiting_icu":
        return False

    acuity = int(current.attributes["acuity"])
    bed_request = f"icu-bed:{current.id}"
    team_request = f"icu-team:{current.id}"
    bed = engine.resources.ensure_requested(
        backend,
        resource_name="icu_bed",
        request_id=bed_request,
        requested_at=backend.now,
        priority=acuity,
    )
    team = engine.resources.ensure_requested(
        backend,
        resource_name="clinical_team",
        request_id=team_request,
        requested_at=backend.now,
        priority=acuity,
    )
    if bed is None or team is None:
        if bed is not None:
            engine.resources.withdraw(backend, bed_request)
        if team is not None:
            engine.resources.withdraw(backend, team_request)
        return False

    current = admission(persistence, current.id)
    dispatch(
        engine,
        current,
        "allocate_icu",
        key=("hospital", current.id, "allocate-icu"),
        correlation_id=flow_correlation_id(current.id),
    )
    return True


def release_icu_capacity(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    admission_id: str,
) -> None:
    for request_id in (
        f"icu-bed:{admission_id}",
        f"icu-team:{admission_id}",
    ):
        engine.resources.withdraw(backend, request_id)


def discharge_from_icu(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    admission_id: str,
) -> None:
    current = admission(persistence, admission_id)
    if current.state == "icu":
        dispatch(
            engine,
            current,
            "ready_discharge",
            key=("hospital", current.id, "icu-ready-discharge"),
            correlation_id=flow_correlation_id(current.id),
        )
        current = admission(persistence, current.id)

    if current.state != "discharge_ready":
        raise RuntimeError(
            f"admission is not discharge ready: {current.state}"
        )

    dispatch(
        engine,
        current,
        "discharge",
        key=("hospital", current.id, "icu-discharge"),
        correlation_id=flow_correlation_id(current.id),
    )
    release_icu_capacity(
        persistence,
        engine,
        backend,
        admission_id=current.id,
    )


def transfer_from_icu_wait(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    admission_id: str,
) -> None:
    current = admission(persistence, admission_id)
    if current.state != "waiting_icu":
        raise RuntimeError("ICU transfer requires waiting_icu")

    for request_id in (
        f"icu-bed:{current.id}",
        f"icu-team:{current.id}",
    ):
        engine.resources.withdraw(backend, request_id)

    current = admission(persistence, current.id)
    dispatch(
        engine,
        current,
        "transfer",
        key=("hospital", current.id, "icu-transfer"),
        correlation_id=flow_correlation_id(current.id),
    )
