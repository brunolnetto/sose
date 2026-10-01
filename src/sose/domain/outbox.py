from __future__ import annotations

from dataclasses import replace

from sose.persistence.base import Persistence

from .delivery import DomainDelivery
from .warehouse import DomainApplyResult, DomainMutation, DomainWarehouse


class DomainMutationOutbox:
    """Recoverable bridge from authoritative Engine OLTP to DomainWarehouse."""

    def __init__(self, persistence: Persistence, warehouse: DomainWarehouse) -> None:
        self.persistence = persistence
        self.warehouse = warehouse

    def enqueue(self, uow, mutation: DomainMutation) -> DomainDelivery:
        delivery = DomainDelivery(mutation)
        existing = uow.get_domain_delivery(mutation.mutation_id)
        if existing is not None:
            if existing.mutation != mutation:
                raise ValueError(
                    f"domain mutation identity conflict: {mutation.mutation_id}"
                )
            return existing
        uow.save_domain_delivery(delivery)
        return delivery

    def prepare(self, mutation: DomainMutation) -> DomainDelivery:
        with self.persistence.transaction() as uow:
            return self.enqueue(uow, mutation)

    def pending(self) -> tuple[DomainDelivery, ...]:
        return self.persistence.domain_deliveries()

    def deliver(self, delivery: DomainDelivery) -> DomainApplyResult | None:
        current = self.persistence.domain_delivery(delivery.mutation_id)
        if current is None:
            return None
        try:
            result = self.warehouse.apply(current.mutation)
        except Exception as exc:
            failed = replace(
                current,
                attempts=current.attempts + 1,
                last_error=f"{type(exc).__name__}: {exc}",
            )
            with self.persistence.transaction() as uow:
                latest = uow.get_domain_delivery(current.mutation_id)
                if latest is not None:
                    uow.save_domain_delivery(failed)
            raise

        # The warehouse may already contain the mutation if the process died after
        # apply() but before this ACK. DomainWarehouse.apply() is deliberately
        # idempotent, so either result is safe to acknowledge.
        with self.persistence.transaction() as uow:
            uow.delete_domain_delivery(current.mutation_id)
        return result

    def flush(self) -> int:
        delivered = 0
        for delivery in self.pending():
            self.deliver(delivery)
            delivered += 1
        return delivered
