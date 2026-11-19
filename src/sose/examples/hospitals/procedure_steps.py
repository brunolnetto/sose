"""Hospital preemption-result lookup and procedure completion (split from procedures.py)."""

from __future__ import annotations

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .runtime import (
    dispatch,
    episode,
    flow_correlation_id,
)


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
