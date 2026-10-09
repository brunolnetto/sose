from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable

from sose.persistence.base import Persistence

from .model import (
    BoundaryConsumption,
    BoundaryDelivery,
    BoundaryLease,
    BoundaryMessage,
    DeliveryStatus,
)

if TYPE_CHECKING:
    from sose.persistence.base import UnitOfWork


class StaleBoundaryClaimError(RuntimeError):
    """Raised when a worker acts with an obsolete boundary-delivery lease."""


class UnsupportedBoundaryContractError(LookupError):
    """Raised when no consumer is registered for a destination contract."""


BoundaryHandler = Callable[[BoundaryMessage, "UnitOfWork"], str]


class BoundaryConsumerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[tuple[str, str, int], BoundaryHandler] = {}

    def register(
        self,
        *,
        destination_domain: str,
        contract_name: str,
        contract_version: int,
        handler: BoundaryHandler,
    ) -> None:
        key = (destination_domain, contract_name, contract_version)
        if not destination_domain or not contract_name or contract_version <= 0:
            raise ValueError("invalid boundary consumer contract")
        if key in self._handlers:
            raise ValueError(
                "boundary consumer already registered: "
                f"{destination_domain}:{contract_name}.v{contract_version}"
            )
        self._handlers[key] = handler

    def contracts_for(self, destination_domain: str) -> frozenset[tuple[str, int]]:
        """Return the registered contract names and versions owned by one domain."""
        return frozenset(
            (contract_name, version)
            for domain, contract_name, version in self._handlers
            if domain == destination_domain
        )

    def resolve(self, message: BoundaryMessage) -> BoundaryHandler:
        key = (
            message.destination_domain,
            message.contract_name,
            message.contract_version,
        )
        handler = self._handlers.get(key)
        if handler is None:
            raise UnsupportedBoundaryContractError(
                "unsupported boundary contract: "
                f"{message.destination_domain}:{message.contract_key}"
            )
        return handler


class BoundaryService:
    """Durable in-process composition boundary over the authoritative SOSE UoW."""

    def __init__(self, persistence: Persistence) -> None:
        self.persistence = persistence

    def publish(self, message: BoundaryMessage) -> BoundaryDelivery:
        delivery = BoundaryDelivery.pending(message)
        with self.persistence.transaction() as uow:
            existing_message = uow.get_boundary_message(message.message_id)
            if existing_message is not None and existing_message != message:
                raise ValueError(
                    f"boundary message identity conflict: {message.message_id}"
                )
            if existing_message is None:
                uow.save_boundary_message(message)

            existing_delivery = uow.get_boundary_delivery(delivery.delivery_id)
            if existing_delivery is not None:
                if (
                    existing_delivery.message_id != message.message_id
                    or existing_delivery.destination_domain != message.destination_domain
                    or existing_delivery.contract_name != message.contract_name
                    or existing_delivery.contract_version != message.contract_version
                ):
                    raise ValueError(
                        f"boundary delivery identity conflict: {delivery.delivery_id}"
                    )
                return existing_delivery
            uow.save_boundary_delivery(delivery)
            return delivery

    def delivery(self, delivery_id: str) -> BoundaryDelivery | None:
        with self.persistence.transaction() as uow:
            return uow.get_boundary_delivery(delivery_id)

    def consumption(self, delivery_id: str) -> BoundaryConsumption | None:
        with self.persistence.transaction() as uow:
            return uow.get_boundary_consumption(delivery_id)

    def claim_next(
        self,
        *,
        owner_id: str,
        now: datetime,
        lease_duration: timedelta,
        destination_domain: str | None = None,
        accepted_contracts: frozenset[tuple[str, int]] | None = None,
    ) -> BoundaryLease | None:
        """Lease only a worker-owned destination when requested.

        The default None preserves the existing global deterministic claim
        order for old single-worker composition references.
        """
        if not owner_id:
            raise ValueError("owner_id cannot be empty")
        if destination_domain is not None and not destination_domain:
            raise ValueError("destination_domain must be nonempty when specified")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if accepted_contracts is not None and any(
            not isinstance(name, str) or not name or not isinstance(version, int)
            or version <= 0
            for name, version in accepted_contracts
        ):
            raise ValueError("accepted_contracts requires nonempty names and positive versions")

        with self.persistence.transaction() as uow:
            candidates: list[tuple[datetime, str, BoundaryDelivery]] = []
            for delivery in uow.boundary_deliveries():
                if destination_domain is not None and (
                    delivery.destination_domain != destination_domain
                ):
                    continue
                if accepted_contracts is not None and (
                    delivery.contract_name, delivery.contract_version
                ) not in accepted_contracts:
                    continue
                if delivery.status is DeliveryStatus.CONSUMED:
                    continue
                if (
                    delivery.status is DeliveryStatus.CLAIMED
                    and delivery.lease_expires_at is not None
                    and delivery.lease_expires_at > now
                ):
                    continue
                message = uow.get_boundary_message(delivery.message_id)
                if message is None:
                    raise RuntimeError(
                        f"boundary delivery missing message: {delivery.delivery_id}"
                    )
                candidates.append((message.produced_at, delivery.delivery_id, delivery))

            if not candidates:
                return None

            _, _, current = min(candidates, key=lambda item: (item[0], item[1]))
            claimed = replace(
                current,
                status=DeliveryStatus.CLAIMED,
                attempts=current.attempts + 1,
                claim_owner_id=owner_id,
                claim_epoch=current.claim_epoch + 1,
                claimed_at=now,
                lease_expires_at=now + lease_duration,
                consumed_at=None,
                consumer_effect_id=None,
                last_error=None,
            )
            uow.save_boundary_delivery(claimed)
            return BoundaryLease(
                delivery_id=claimed.delivery_id,
                message_id=claimed.message_id,
                owner_id=owner_id,
                epoch=claimed.claim_epoch,
                lease_expires_at=claimed.lease_expires_at,
            )

    def consume(
        self,
        *,
        lease: BoundaryLease,
        registry: BoundaryConsumerRegistry,
        now: datetime,
    ) -> BoundaryConsumption:
        with self.persistence.transaction() as uow:
            current = uow.get_boundary_delivery(lease.delivery_id)
            if current is None:
                raise KeyError(f"unknown boundary delivery: {lease.delivery_id}")

            existing = uow.get_boundary_consumption(current.delivery_id)
            if current.status is DeliveryStatus.CONSUMED:
                if existing is None:
                    raise RuntimeError(
                        f"consumed delivery lacks consumption record: {current.delivery_id}"
                    )
                return existing

            self._assert_current_claim(current, lease=lease, now=now)
            message = uow.get_boundary_message(current.message_id)
            if message is None:
                raise RuntimeError(
                    f"boundary delivery missing message: {current.delivery_id}"
                )

            handler = registry.resolve(message)
            effect_id = handler(message, uow)
            if not effect_id:
                raise ValueError("boundary consumer effect identity cannot be empty")

            consumption = BoundaryConsumption.create(
                delivery=current,
                consumer_effect_id=effect_id,
                consumed_at=now,
            )
            if existing is not None and existing != consumption:
                raise ValueError(
                    f"boundary consumption identity conflict: {current.delivery_id}"
                )
            if existing is None:
                uow.save_boundary_consumption(consumption)

            uow.save_boundary_delivery(
                replace(
                    current,
                    status=DeliveryStatus.CONSUMED,
                    consumed_at=now,
                    consumer_effect_id=effect_id,
                )
            )
            return consumption

    @staticmethod
    def _assert_current_claim(
        current: BoundaryDelivery,
        *,
        lease: BoundaryLease,
        now: datetime,
    ) -> None:
        if current.status is not DeliveryStatus.CLAIMED:
            raise StaleBoundaryClaimError(
                f"boundary delivery is not claimed: {current.delivery_id}"
            )
        if (
            current.message_id != lease.message_id
            or current.claim_owner_id != lease.owner_id
            or current.claim_epoch != lease.epoch
        ):
            raise StaleBoundaryClaimError(
                f"stale boundary claim: {current.delivery_id}"
            )
        if current.lease_expires_at is None or current.lease_expires_at <= now:
            raise StaleBoundaryClaimError(
                f"expired boundary claim: {current.delivery_id}"
            )


__all__ = [
    "BoundaryConsumerRegistry",
    "BoundaryConsumption",
    "BoundaryDelivery",
    "BoundaryLease",
    "BoundaryMessage",
    "BoundaryService",
    "DeliveryStatus",
    "StaleBoundaryClaimError",
    "UnsupportedBoundaryContractError",
]
