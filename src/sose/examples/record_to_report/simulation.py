from __future__ import annotations


from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.core.randomness import RandomSource  # noqa: F401
from sose.persistence.memory import MemoryPersistence

from .identity import flow_correlation_id
from .scenarios import ORIGIN
from .foundation import (
    R2REntities,
    REOPEN_CLOSE_DELAY,
    _dispatch,
    _journal,
    _reconciliation,
    build_runtime,
    seed_reference,
)
from .adjustments import post_adjustment
from .close import reconcile_close, schedule_close

__all__ = [
    "ORIGIN", "R2REntities", "REOPEN_CLOSE_DELAY", "build_runtime", "flow_correlation_id",
    "post_adjustment", "reconcile_close", "reconcile_item", "run_adjustment_path",
    "run_happy_path", "schedule_close", "seed_reference", "submit_and_post_journal",
]


def submit_and_post_journal(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: R2REntities,
    reject: bool = False,
) -> bool:
    journal = _journal(persistence, entities)
    request_id = f"posting:{journal.id}"
    if journal.state in {"posted", "rejected"}:
        engine.resources.withdraw(backend, request_id)
        return journal.state == "posted"

    available = bool(
        engine.context.scenarios.attribute("r2r.posting.available", True)
    )
    if not available:
        engine.resources.withdraw(backend, request_id)
        return False

    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="posting_processor",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    correlation_id = flow_correlation_id(entities.period_id)
    journal = _journal(persistence, entities)
    if journal.state == "drafted":
        _dispatch(
            engine,
            journal,
            "submit",
            key=("r2r", journal.id, "submit"),
            correlation_id=correlation_id,
        )
        journal = _journal(persistence, entities)

    if journal.state == "submitted":
        _dispatch(
            engine,
            journal,
            "reject" if reject else "post",
            key=("r2r", journal.id, "reject" if reject else "post"),
            correlation_id=correlation_id,
        )

    engine.resources.withdraw(backend, request_id)
    return _journal(persistence, entities).state == "posted"


def reconcile_item(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: R2REntities,
    outcome: str = "match",
) -> bool:
    if outcome not in {"match", "unmatched"}:
        raise ValueError(f"unsupported reconciliation outcome: {outcome}")

    journal = _journal(persistence, entities)
    if journal.state != "posted":
        raise RuntimeError("reconciliation requires durable posted journal evidence")

    item = _reconciliation(persistence, entities)
    request_id = f"reconciliation-analyst:{item.id}"
    if item.state in {"matched", "reconciled", "rejected"}:
        engine.resources.withdraw(backend, request_id)
        return item.state in {"matched", "reconciled"}

    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="reconciliation_analyst",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    correlation_id = flow_correlation_id(entities.period_id)
    item = _reconciliation(persistence, entities)
    if item.state == "pending":
        _dispatch(
            engine,
            item,
            "start",
            key=("r2r", item.id, "start"),
            correlation_id=correlation_id,
        )
        item = _reconciliation(persistence, entities)

    if item.state == "reconciling":
        event = "match" if outcome == "match" else "mark_unmatched"
        _dispatch(
            engine,
            item,
            event,
            key=("r2r", item.id, event),
            correlation_id=correlation_id,
        )

    engine.resources.withdraw(backend, request_id)
    return _reconciliation(persistence, entities).state == "matched"


def run_happy_path(*, owner_id: str) -> tuple[MemoryPersistence, R2REntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, owner_id=owner_id)
    _, engine = build_runtime(persistence, owner_id=owner_id)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    if not submit_and_post_journal(
        persistence,
        engine,
        backend,
        entities=entities,
    ):
        raise RuntimeError("journal posting failed")
    if not reconcile_item(
        persistence,
        engine,
        backend,
        entities=entities,
        outcome="match",
    ):
        raise RuntimeError("reconciliation failed")

    due_at = schedule_close(
        persistence,
        engine,
        backend,
        task_id=entities.close_task_id,
        period_id=entities.period_id,
    )
    backend.run_until(due_at)
    if not reconcile_close(
        persistence,
        engine,
        backend,
        entities=entities,
    ):
        raise RuntimeError("period close failed")
    return persistence, entities


def run_adjustment_path(*, owner_id: str) -> tuple[MemoryPersistence, R2REntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, owner_id=owner_id)
    _, engine = build_runtime(persistence, owner_id=owner_id)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    if not submit_and_post_journal(
        persistence,
        engine,
        backend,
        entities=entities,
    ):
        raise RuntimeError("journal posting failed")
    reconcile_item(
        persistence,
        engine,
        backend,
        entities=entities,
        outcome="unmatched",
    )
    if not post_adjustment(
        persistence,
        engine,
        backend,
        entities=entities,
    ):
        raise RuntimeError("adjustment posting failed")

    due_at = schedule_close(
        persistence,
        engine,
        backend,
        task_id=entities.close_task_id,
        period_id=entities.period_id,
    )
    backend.run_until(due_at)
    if not reconcile_close(
        persistence,
        engine,
        backend,
        entities=entities,
    ):
        raise RuntimeError("period close failed")
    return persistence, entities
