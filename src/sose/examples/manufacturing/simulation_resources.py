"""Manufacturing setup capacity, breakdown and repair (split from simulation.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .simulation_runtime import (
    flow_correlation_id,
)

if TYPE_CHECKING:
    from .simulation import ManufacturingEntities


def reconcile_setup_resources(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ManufacturingEntities,
) -> bool:
    """Advance into setup only while machine and operator are durably held."""

    machine_request = f"machine:{entities.production_order_id}"
    operator_request = f"operator:{entities.production_order_id}"

    machine = engine.preemptive_resources.ensure_requested(
        backend,
        resource_name="machine",
        request_id=machine_request,
        requested_at=backend.now,
        priority=100,
        preempt=False,
    )
    operator = engine.resources.ensure_requested(
        backend,
        resource_name="operator",
        request_id=operator_request,
        requested_at=backend.now,
        priority=100,
    )
    if machine is None or operator is None:
        return False

    order = persistence.entity("production_order", entities.production_order_id)
    operation = persistence.entity("manufacturing_operation", entities.operation_id)
    if order is None or operation is None:
        raise RuntimeError("manufacturing entities were not persisted")

    if order.state == "released":
        command = engine.context.commands.create(
            "begin_setup",
            target=order,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-reference", order.id, "begin-setup"),
        )
        engine.dispatch(command)

    if operation.state == "ready_state":
        command = engine.context.commands.create(
            "start",
            target=operation,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-reference", operation.id, "start"),
        )
        engine.dispatch(command)

    return True


def release_setup_resources(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ManufacturingEntities,
) -> None:
    engine.preemptive_resources.withdraw(
        backend,
        f"machine:{entities.production_order_id}",
    )
    engine.resources.withdraw(
        backend,
        f"operator:{entities.production_order_id}",
    )


def reconcile_scenario_breakdown(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ManufacturingEntities,
) -> bool:
    """Translate scenario context into the explicit durable breakdown workflow."""

    if not engine.context.scenarios.attribute("manufacturing.machine.down", False):
        return False
    return reconcile_breakdown(
        persistence, engine, backend, entities=entities
    )


def reconcile_breakdown(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ManufacturingEntities,
) -> bool:
    """Preempt production capacity and make the breakdown durable in business state."""

    repair_request = f"machine-repair:{entities.production_order_id}"
    repair = engine.preemptive_resources.ensure_requested(
        backend,
        resource_name="machine",
        request_id=repair_request,
        requested_at=backend.now,
        priority=1,
        preempt=True,
    )
    if repair is None:
        return False

    preempted = any(
        result.preempting_request_id == repair_request
        for result in persistence.resource_preemption_results()
    )
    if not preempted:
        return False

    order = persistence.entity("production_order", entities.production_order_id)
    operation = persistence.entity("manufacturing_operation", entities.operation_id)
    if order is None or operation is None:
        raise RuntimeError("manufacturing entities were not persisted")

    if order.state in {"setup", "producing"}:
        command = engine.context.commands.create(
            "breakdown",
            target=order,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-breakdown", order.id, "breakdown"),
        )
        engine.dispatch(command)

    operation = persistence.entity("manufacturing_operation", entities.operation_id)
    if operation is not None and operation.state in {"ready_state", "running"}:
        command = engine.context.commands.create(
            "block",
            target=operation,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-breakdown", operation.id, "block"),
        )
        engine.dispatch(command)

    return True


def reconcile_repair(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ManufacturingEntities,
) -> bool:
    """Release emergency repair and reacquire machine before resuming production."""

    repair_request = f"machine-repair:{entities.production_order_id}"
    production_request = f"machine:{entities.production_order_id}"

    engine.preemptive_resources.withdraw(backend, repair_request)

    machine = engine.preemptive_resources.ensure_requested(
        backend,
        resource_name="machine",
        request_id=production_request,
        requested_at=backend.now,
        priority=100,
        preempt=False,
    )
    if machine is None:
        return False

    order = persistence.entity("production_order", entities.production_order_id)
    operation = persistence.entity("manufacturing_operation", entities.operation_id)
    if order is None or operation is None:
        raise RuntimeError("manufacturing entities were not persisted")

    if order.state == "machine_down":
        command = engine.context.commands.create(
            "repair",
            target=order,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-breakdown", order.id, "repair"),
        )
        engine.dispatch(command)

    operation = persistence.entity("manufacturing_operation", entities.operation_id)
    if operation is not None and operation.state == "blocked":
        command = engine.context.commands.create(
            "unblock",
            target=operation,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-breakdown", operation.id, "unblock"),
        )
        engine.dispatch(command)
        operation = persistence.entity("manufacturing_operation", entities.operation_id)

    if operation is not None and operation.state == "ready_state":
        command = engine.context.commands.create(
            "start",
            target=operation,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-breakdown", operation.id, "restart"),
        )
        engine.dispatch(command)

    return True
