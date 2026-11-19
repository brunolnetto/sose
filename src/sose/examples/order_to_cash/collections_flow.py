"""Order-to-cash collection case and receivable collection (split from simulation.py).

Collaborators are resolved through ``simulation`` at call time so monkeypatches
applied to that module keep steering these functions.
"""

from __future__ import annotations

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .entities import CollectionCase
from .records import COLLECTION_FOLLOWUP_DELAY, O2CEntities, flow_correlation_id


def _sim():
    from . import simulation

    return simulation


def ensure_collection_case(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: O2CEntities,
) -> CollectionCase:
    receivable = _sim()._receivable(persistence, entities.order_id)
    if receivable is None:
        raise RuntimeError("receivable was not persisted")
    existing = _sim()._collection_case(persistence, receivable.id)
    if existing is not None:
        return existing
    if receivable.state != "overdue":
        raise RuntimeError(
            f"collection case requires Receivable(overdue), got {receivable.state}"
        )

    value = engine.context.entities.create(
        CollectionCase,
        key=("o2c-reference", receivable.id, "collection-case"),
        state="opened",
        attributes={"receivable_id": receivable.id},
    )
    with persistence.transaction() as uow:
        uow.save_entity(value)
    return value


def reconcile_collection(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: O2CEntities,
    promise: bool = False,
) -> bool:
    case = _sim().ensure_collection_case(
        persistence,
        engine,
        entities=entities,
    )
    request_id = f"collection-agent:{case.id}"
    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="collection_agent",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    correlation_id = flow_correlation_id(entities.order_id)
    case = _sim()._collection_case(
        persistence,
        str(case.attributes["receivable_id"]),
    )
    if case is None:
        raise RuntimeError("collection case disappeared")
    if case.state == "opened":
        _sim()._dispatch(
            engine,
            case,
            "assign",
            key=("o2c-collection", case.id, "assign"),
            correlation_id=correlation_id,
        )
        case = persistence.entity("collection_case", case.id)
    if case is not None and case.state == "assigned":
        _sim()._dispatch(
            engine,
            case,
            "contact",
            key=("o2c-collection", case.id, "contact"),
            correlation_id=correlation_id,
        )
        case = persistence.entity("collection_case", case.id)

    if promise and case is not None and case.state == "contacted":
        _sim()._dispatch(
            engine,
            case,
            "promise",
            key=("o2c-collection", case.id, "promise"),
            correlation_id=correlation_id,
        )
        case = persistence.entity("collection_case", case.id)
        due_at = backend.now + COLLECTION_FOLLOWUP_DELAY
        command = engine.context.commands.create(
            "escalate",
            target=case,
            due_at=due_at,
            correlation_id=correlation_id,
            key=("o2c-collection", case.id, "followup"),
        )
        engine.context.schedules.at(due_at, command=command)

    engine.resources.withdraw(backend, request_id)
    return True


def collect_receivable(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: O2CEntities,
) -> bool:
    receivable = _sim()._receivable(persistence, entities.order_id)
    if receivable is None:
        return False
    if receivable.state == "collected":
        return True
    if receivable.state not in {"due", "overdue"}:
        return False

    _sim()._dispatch(
        engine,
        receivable,
        "collect",
        key=("o2c", receivable.id, "collect"),
        correlation_id=flow_correlation_id(entities.order_id),
    )
    engine.scheduler.cancel_pending(
        entity_type="receivable",
        entity_id=receivable.id,
        name="mark_overdue",
    )

    case = _sim()._collection_case(persistence, receivable.id)
    if case is not None and case.state in {"contacted", "promised", "escalated"}:
        _sim()._dispatch(
            engine,
            case,
            "resolve",
            key=("o2c-collection", case.id, "resolve"),
            correlation_id=flow_correlation_id(entities.order_id),
        )
        engine.scheduler.cancel_pending(
            entity_type="collection_case",
            entity_id=case.id,
            name="escalate",
        )
    return True
