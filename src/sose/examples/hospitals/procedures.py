from __future__ import annotations

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .runtime import (
    admission,
    dispatch,
    emergency_episode_id,
    episode,
    flow_correlation_id,
    maybe_episode,
)


def queue_treatment_episode(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    episode_id: str,
) -> None:
    current = episode(persistence, episode_id)
    parent = admission(
        persistence,
        str(current.attributes["admission_id"]),
    )
    if parent.state not in {"treatment", "icu"}:
        raise RuntimeError(
            f"procedure requires Admission(treatment|icu), got {parent.state}"
        )
    if current.state == "planned":
        dispatch(
            engine,
            current,
            "queue",
            key=("hospital-procedure", current.id, "queue"),
            correlation_id=flow_correlation_id(
                str(current.attributes["admission_id"])
            ),
        )


def reconcile_procedure_start(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    episode_id: str,
) -> bool:
    current = episode(persistence, episode_id)
    parent = admission(
        persistence,
        str(current.attributes["admission_id"]),
    )
    if parent.state not in {"treatment", "icu"}:
        return False

    if current.state == "planned":
        queue_treatment_episode(
            persistence,
            engine,
            episode_id=current.id,
        )
        current = episode(persistence, current.id)

    if current.state == "in_progress":
        return True
    if current.state != "waiting_capacity":
        return False

    request_id = f"procedure:{current.id}"
    reservation = engine.preemptive_resources.ensure_requested(
        backend,
        resource_name="procedure_suite",
        request_id=request_id,
        requested_at=backend.now,
        priority=100,
        preempt=False,
    )
    if reservation is None:
        return False

    current = episode(persistence, current.id)
    if current.state == "waiting_capacity":
        dispatch(
            engine,
            current,
            "start",
            key=("hospital-procedure", current.id, "start"),
            correlation_id=flow_correlation_id(
                str(current.attributes["admission_id"])
            ),
        )
    return True


def ensure_emergency_episode(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    normal_episode_id: str,
):
    emergency_id = emergency_episode_id(normal_episode_id)
    existing = maybe_episode(persistence, emergency_id)
    if existing is not None:
        return existing

    normal = episode(persistence, normal_episode_id)
    emergency = engine.context.entities.create(
        type(normal),
        key=("hospital-reference", normal.id, "emergency-1"),
        state="planned",
        attributes={
            "admission_id": normal.attributes["admission_id"],
            "kind": "emergency-procedure",
            "emergency": True,
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(emergency)
    return emergency


def emergency_request_id(normal_episode_id: str) -> str:
    return f"procedure-emergency:{normal_episode_id}:1"


def preemption_result(
    persistence: MemoryPersistence,
    normal_episode_id: str,
):
    normal_request = f"procedure:{normal_episode_id}"
    emergency_request = emergency_request_id(normal_episode_id)
    return next(
        (
            result
            for result in persistence.resource_preemption_results()
            if result.displaced_request_id == normal_request
            and result.preempting_request_id == emergency_request
        ),
        None,
    )


def _dispatch_emergency_start_if_waiting(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    emergency_id: str,
    normal_id: str,
) -> bool:
    emergency = maybe_episode(persistence, emergency_id)
    reservation = engine.preemptive_resources.reservation_for(
        emergency_request_id(normal_id)
    )
    if (
        emergency is None
        or emergency.state != "waiting_capacity"
        or reservation is None
    ):
        return False
    dispatch(
        engine,
        emergency,
        "start",
        key=("hospital-emergency", emergency.id, "start"),
        correlation_id=flow_correlation_id(
            str(emergency.attributes["admission_id"])
        ),
    )
    return True


def _ensure_emergency_episode_ready(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    normal_episode_id: str,
):
    emergency = ensure_emergency_episode(
        persistence,
        engine,
        normal_episode_id=normal_episode_id,
    )
    if emergency.state != "planned":
        return emergency
    queue_treatment_episode(
        persistence,
        engine,
        episode_id=emergency.id,
    )
    return episode(persistence, emergency.id)


def commit_emergency_preemption(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    normal_episode_id: str,
) -> bool:
    normal = episode(persistence, normal_episode_id)
    if normal.state != "in_progress":
        return normal.state == "interrupted"

    normal_request = f"procedure:{normal.id}"
    if engine.preemptive_resources.reservation_for(normal_request) is None:
        result = preemption_result(persistence, normal.id)
        if result is None:
            return False
        _dispatch_emergency_start_if_waiting(
            persistence,
            engine,
            emergency_id=emergency_episode_id(normal.id),
            normal_id=normal.id,
        )
        return True

    emergency = _ensure_emergency_episode_ready(
        persistence,
        engine,
        normal_episode_id=normal.id,
    )

    request_id = emergency_request_id(normal.id)
    reservation = engine.preemptive_resources.ensure_requested(
        backend,
        resource_name="procedure_suite",
        request_id=request_id,
        requested_at=backend.now,
        priority=1,
        preempt=True,
    )
    result = preemption_result(persistence, normal.id)
    if reservation is None or result is None:
        return False

    _dispatch_emergency_start_if_waiting(
        persistence,
        engine,
        emergency_id=emergency.id,
        normal_id=normal.id,
    )
    return True


def reconcile_preemption_business(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    normal_episode_id: str,
) -> bool:
    normal = episode(persistence, normal_episode_id)
    if normal.state == "interrupted":
        return True
    if normal.state != "in_progress":
        return False

    result = preemption_result(persistence, normal.id)
    normal_reservation = engine.preemptive_resources.reservation_for(
        f"procedure:{normal.id}"
    )
    if result is None or normal_reservation is not None:
        return False

    dispatch(
        engine,
        normal,
        "interrupt",
        key=(
            "hospital-preemption",
            normal.id,
            result.preempting_request_id,
            "interrupt",
        ),
        correlation_id=flow_correlation_id(
            str(normal.attributes["admission_id"])
        ),
    )
    return True


def complete_emergency_and_resume(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    normal_episode_id: str,
) -> bool:
    if not reconcile_preemption_business(
        persistence,
        engine,
        normal_episode_id=normal_episode_id,
    ):
        normal = episode(persistence, normal_episode_id)
        if normal.state != "interrupted":
            return False

    emergency = episode(
        persistence,
        emergency_episode_id(normal_episode_id),
    )
    correlation_id = flow_correlation_id(
        str(emergency.attributes["admission_id"])
    )
    emergency_request = emergency_request_id(normal_episode_id)
    if (
        emergency.state == "waiting_capacity"
        and engine.preemptive_resources.reservation_for(emergency_request) is not None
    ):
        dispatch(
            engine,
            emergency,
            "start",
            key=("hospital-emergency", emergency.id, "start"),
            correlation_id=correlation_id,
        )
        emergency = episode(persistence, emergency.id)

    if emergency.state == "in_progress":
        dispatch(
            engine,
            emergency,
            "complete",
            key=("hospital-emergency", emergency.id, "complete"),
            correlation_id=correlation_id,
        )
        emergency = episode(persistence, emergency.id)

    if emergency.state != "completed":
        return False

    engine.preemptive_resources.withdraw(
        backend,
        emergency_request_id(normal_episode_id),
    )

    normal_request = f"procedure:{normal_episode_id}"
    reservation = engine.preemptive_resources.ensure_requested(
        backend,
        resource_name="procedure_suite",
        request_id=normal_request,
        requested_at=backend.now,
        priority=100,
        preempt=False,
    )
    if reservation is None:
        return False

    normal = episode(persistence, normal_episode_id)
    if normal.state == "interrupted":
        dispatch(
            engine,
            normal,
            "resume",
            key=("hospital-procedure", normal.id, "resume"),
            correlation_id=flow_correlation_id(
                str(normal.attributes["admission_id"])
            ),
        )
    return episode(persistence, normal_episode_id).state == "in_progress"


def complete_procedure(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    episode_id: str,
) -> None:
    current = episode(persistence, episode_id)
    if current.state != "in_progress":
        raise RuntimeError(f"procedure is not active: {current.state}")

    dispatch(
        engine,
        current,
        "complete",
        key=("hospital-procedure", current.id, "complete"),
        correlation_id=flow_correlation_id(
            str(current.attributes["admission_id"])
        ),
    )
    engine.preemptive_resources.withdraw(
        backend,
        f"procedure:{current.id}",
    )


def reconcile_scenario_emergency(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    normal_episode_id: str,
) -> bool:
    active = bool(
        engine.context.scenarios.attribute(
            "hospital.procedure.emergency",
            False,
        )
    )
    emergency = maybe_episode(
        persistence,
        emergency_episode_id(normal_episode_id),
    )

    if active:
        committed = commit_emergency_preemption(
            persistence,
            engine,
            backend,
            normal_episode_id=normal_episode_id,
        )
        if committed:
            reconcile_preemption_business(
                persistence,
                engine,
                normal_episode_id=normal_episode_id,
            )
        return committed

    if emergency is None:
        return False

    normal = episode(persistence, normal_episode_id)
    if emergency.state == "completed" and normal.state == "in_progress":
        return False

    return complete_emergency_and_resume(
        persistence,
        engine,
        backend,
        normal_episode_id=normal_episode_id,
    )
