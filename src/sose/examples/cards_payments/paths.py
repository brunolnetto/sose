"""Reference cards-payments runner paths (split from simulation.py).

Collaborators are resolved through ``simulation`` at call time so monkeypatches
applied to that module keep steering these paths.
"""

from __future__ import annotations

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .records import PaymentEntities
from .scenarios import ORIGIN


def _sim():
    from . import simulation

    return simulation


def _prepare_authorized(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
) -> tuple[PaymentEntities, Engine, SimPyBackend]:
    entities = _sim().seed_reference(persistence, owner_id=owner_id)
    _, engine = _sim().build_runtime(persistence, owner_id=owner_id)
    backend = _sim().SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    if not _sim().reconcile_authorization(
        persistence, engine, backend, entities=entities, outcome="authorize"
    ):
        raise RuntimeError("authorization capacity unavailable")
    return entities, engine, backend


def run_happy_path(*, owner_id: str) -> tuple[MemoryPersistence, PaymentEntities]:
    persistence = _sim().MemoryPersistence()
    entities, engine, backend = _sim()._prepare_authorized(persistence, owner_id=owner_id)
    settlement_at = _sim().reconcile_capture_and_schedule_settlement(
        persistence, engine, backend, entities=entities
    )
    backend.run_until(settlement_at)
    _sim().reconcile_settlement(
        persistence, engine, backend, entities=entities, outcome="success"
    )
    return persistence, entities


def run_retry_path(*, owner_id: str) -> tuple[MemoryPersistence, PaymentEntities]:
    persistence = _sim().MemoryPersistence()
    entities, engine, backend = _sim()._prepare_authorized(persistence, owner_id=owner_id)
    settlement_at = _sim().reconcile_capture_and_schedule_settlement(
        persistence, engine, backend, entities=entities
    )
    backend.run_until(settlement_at)
    retry_at = _sim().reconcile_settlement(
        persistence, engine, backend, entities=entities, outcome="retry"
    )
    if retry_at is None:
        raise RuntimeError("settlement retry was not scheduled")
    backend.run_until(retry_at)
    _sim().reconcile_settlement(
        persistence, engine, backend, entities=entities, outcome="success"
    )
    return persistence, entities


def run_refund_path(*, owner_id: str) -> tuple[MemoryPersistence, PaymentEntities]:
    persistence, entities = _sim().run_happy_path(owner_id=owner_id)
    position = persistence.simulation_position(owner_id=owner_id)
    now = position.logical_time if position is not None else ORIGIN
    tick = position.logical_tick if position is not None else 0
    _, engine = _sim().build_runtime(persistence, owner_id=owner_id, now=now, tick=tick)
    backend = _sim().SimPyBackend(origin=now)
    engine.rebuild_backend(backend)
    if not _sim().reconcile_refund(persistence, engine, backend, entities=entities):
        raise RuntimeError("refund capacity unavailable")
    return persistence, entities


def run_dispute_path(
    *,
    owner_id: str,
    outcome: str = "merchant",
) -> tuple[MemoryPersistence, PaymentEntities]:
    persistence, entities = _sim().run_happy_path(owner_id=owner_id)
    position = persistence.simulation_position(owner_id=owner_id)
    now = position.logical_time if position is not None else ORIGIN
    tick = position.logical_tick if position is not None else 0
    _, engine = _sim().build_runtime(persistence, owner_id=owner_id, now=now, tick=tick)
    backend = _sim().SimPyBackend(origin=now)
    engine.rebuild_backend(backend)
    evidence_at = _sim().start_dispute(
        persistence, engine, backend, entities=entities
    )
    backend.run_until(evidence_at)
    if not _sim().reconcile_dispute(
        persistence, engine, backend, outcome=outcome
    ):
        raise RuntimeError("dispute analyst unavailable")
    return persistence, entities
