"""Procure-to-pay receiving dock/inspection reconciliation (split from simulation.py)."""

from __future__ import annotations

from datetime import datetime

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .foundation import (
    P2PEntities,
    flow_correlation_id,
)


def request_receiving_slot(
    engine: Engine,
    backend: SimPyBackend,
    *,
    receipt_id: str,
    requested_at: datetime,
    priority: int = 100,
):
    return engine.resources.ensure_requested(
        backend,
        resource_name="receiving_dock",
        request_id=f"receiving-dock:{receipt_id}",
        requested_at=requested_at,
        priority=priority,
    )


def request_inspector(
    engine: Engine,
    backend: SimPyBackend,
    *,
    receipt_id: str,
    requested_at: datetime,
    priority: int = 100,
):
    return engine.resources.ensure_requested(
        backend,
        resource_name="inspector",
        request_id=f"inspector:{receipt_id}",
        requested_at=requested_at,
        priority=priority,
    )


def reconcile_receiving_resources(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: P2PEntities,
    outcome: str = "accepted",
) -> bool:
    """Advance receipt lifecycle only while required durable capacity is held.

    outcome controls the explicit business branch after dock acquisition:
    accepted -> inspection;
    partial -> mark partial, then inspection;
    rejected -> reject before inventory effects.
    """

    _validate_receiving_outcome(outcome)

    correlation_id = flow_correlation_id()
    dock_request_id = f"receiving-dock:{entities.receipt_id}"
    inspector_request_id = f"inspector:{entities.receipt_id}"

    receipt = _receipt_or_error(persistence, entities.receipt_id)
    if not _ensure_receiving_dock_if_pending(
        engine,
        backend,
        receipt=receipt,
        receipt_id=entities.receipt_id,
    ):
        return False

    receipt, completed = _apply_preinspection_receipt_transitions(
        persistence,
        engine,
        backend,
        receipt=receipt,
        receipt_id=entities.receipt_id,
        outcome=outcome,
        correlation_id=correlation_id,
        dock_request_id=dock_request_id,
    )
    if completed:
        return True

    if not _ensure_inspector_if_required(
        engine,
        backend,
        receipt=receipt,
        receipt_id=entities.receipt_id,
    ):
        return False

    if receipt.state in {"receiving", "partial"}:
        _dispatch_receipt_event(
            engine,
            receipt,
            event="inspect",
            key=("p2p-receiving", receipt.id, "inspect"),
            correlation_id=correlation_id,
        )
        receipt = _receipt_or_error(persistence, entities.receipt_id)
    return _finalize_receiving_result(
        engine,
        backend,
        receipt=receipt,
        inspector_request_id=inspector_request_id,
        dock_request_id=dock_request_id,
    )


def _validate_receiving_outcome(outcome: str) -> None:
    if outcome not in {"accepted", "partial", "rejected"}:
        raise ValueError(f"unsupported receipt outcome: {outcome}")


def _receipt_or_error(persistence: MemoryPersistence, receipt_id: str):
    receipt = persistence.entity("receipt", receipt_id)
    if receipt is None:
        raise RuntimeError("receipt was not persisted")
    return receipt


def _dispatch_receipt_event(
    engine: Engine,
    receipt,
    *,
    event: str,
    key: tuple[object, ...],
    correlation_id: str,
) -> None:
    command = engine.context.commands.create(
        event,
        target=receipt,
        correlation_id=correlation_id,
        key=key,
    )
    engine.dispatch(command)


def _ensure_receiving_dock_if_pending(
    engine: Engine,
    backend: SimPyBackend,
    *,
    receipt,
    receipt_id: str,
) -> bool:
    if receipt.state != "pending":
        return True
    dock = request_receiving_slot(
        engine,
        backend,
        receipt_id=receipt_id,
        requested_at=backend.now,
    )
    return dock is not None


def _ensure_inspector_if_required(
    engine: Engine,
    backend: SimPyBackend,
    *,
    receipt,
    receipt_id: str,
) -> bool:
    if receipt.state not in {"receiving", "partial"}:
        return True
    inspector = request_inspector(
        engine,
        backend,
        receipt_id=receipt_id,
        requested_at=backend.now,
    )
    return inspector is not None


def _apply_preinspection_receipt_transitions(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    receipt,
    receipt_id: str,
    outcome: str,
    correlation_id: str,
    dock_request_id: str,
):
    if receipt.state == "pending":
        _dispatch_receipt_event(
            engine,
            receipt,
            event="begin_receiving",
            key=("p2p-receiving", receipt.id, "begin"),
            correlation_id=correlation_id,
        )
        receipt = _receipt_or_error(persistence, receipt_id)

    if receipt.state == "receiving" and outcome == "rejected":
        _dispatch_receipt_event(
            engine,
            receipt,
            event="reject",
            key=("p2p-receiving", receipt.id, "reject"),
            correlation_id=correlation_id,
        )
        engine.resources.withdraw(backend, dock_request_id)
        return _receipt_or_error(persistence, receipt_id), True

    if receipt.state == "receiving" and outcome == "partial":
        _dispatch_receipt_event(
            engine,
            receipt,
            event="mark_partial",
            key=("p2p-receiving", receipt.id, "partial"),
            correlation_id=correlation_id,
        )
        receipt = _receipt_or_error(persistence, receipt_id)
    return receipt, False


def _finalize_receiving_result(
    engine: Engine,
    backend: SimPyBackend,
    *,
    receipt,
    inspector_request_id: str,
    dock_request_id: str,
) -> bool:
    if receipt.state == "inspected":
        for request_id in (inspector_request_id, dock_request_id):
            engine.resources.withdraw(backend, request_id)
        return True
    return receipt.state == "stocked"
