"""Boundary causal-ancestry validation mixin, split from ``sose.composition.boundary``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .model import BoundaryMessage, DeliveryStatus

if TYPE_CHECKING:
    from sose.persistence.base import UnitOfWork


class _BoundaryCausationMixin:
    """Durable causation checks shared by ``BoundaryService`` transactions."""

    @staticmethod
    def _validate_causation(message: BoundaryMessage, uow: "UnitOfWork") -> None:
        """Follow durable typed/untyped ancestry inside the transaction.

        The optional discriminator lives in the message record, not an
        overloaded identifier prefix. An untyped external ID like 'event:abc'
        is never silently reinterpreted after a code upgrade.
        """
        visited = {message.message_id}
        cursor = message.causation_id
        kind = message.causation_kind
        while cursor is not None:
            if kind == "event":
                event = uow.get_event(cursor)
                if event is None:
                    raise ValueError(f"missing durable domain event cause: {cursor}")
                if event.correlation_id != message.correlation_id:
                    raise ValueError("domain event causation correlation mismatch")
                if event.occurred_at > message.produced_at:
                    raise ValueError("boundary causal logical time precedes domain event")
                return
            if cursor in visited:
                raise ValueError("boundary causal cycle detected")
            visited.add(cursor)
            predecessor = uow.get_boundary_message(cursor)
            if predecessor is None:
                break
            if predecessor.correlation_id != message.correlation_id:
                raise ValueError("boundary causation correlation mismatch")
            if predecessor.produced_at > message.produced_at:
                raise ValueError("boundary causal logical time precedes predecessor")
            cursor = predecessor.causation_id
            kind = predecessor.causation_kind

    @staticmethod
    def _validate_existing_descendants(message: BoundaryMessage, uow: "UnitOfWork") -> None:
        """A late parent must not contradict descendants already durable."""
        for delivery in uow.boundary_deliveries():
            child = uow.get_boundary_message(delivery.message_id)
            if child is None or child.causation_kind == "event":
                continue
            if child.causation_id != message.message_id:
                continue
            if child.correlation_id != message.correlation_id:
                raise ValueError("boundary causation correlation mismatch")
            if child.produced_at < message.produced_at:
                raise ValueError("boundary causal logical time precedes predecessor")
            if delivery.status is DeliveryStatus.CONSUMED:
                raise ValueError("causal descendant already consumed before predecessor")
