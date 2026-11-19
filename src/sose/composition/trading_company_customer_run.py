"""`run_customer_demand_path` boundary progress, split from trading_company_customer."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sose.persistence.memory import MemoryPersistence

    from .model import BoundaryMessage
    from .trading_company_customer_support import _CustomerFixtures

# Every collaborator below is resolved through `trading_company_customer` at call
# time, so names replaced on that module (`_publish`, `_execute_intent`, `monotonic`,
# `sleep`, ...) keep governing this run exactly as before the split.


class _DemandPathRun:
    """Boundary progress of one synchronous `run_customer_demand_path` call."""

    def __init__(
        self,
        persistence: MemoryPersistence,
        *,
        fixtures: _CustomerFixtures,
        correlation_id: str,
        organization_id: str,
        origin: datetime,
    ) -> None:
        from sose.composition import trading_company_customer as customer

        self.persistence = persistence
        self.fixtures = fixtures
        self.correlation_id = correlation_id
        self.organization_id = organization_id
        self.origin = origin
        self.service = customer.BoundaryService(persistence)
        self.registry = customer._registry(fixtures, owner_id=organization_id)
        self.messages: list[BoundaryMessage] = []
        self.effects: list[str] = []

    def consume_published_boundary(self, message: BoundaryMessage, *, owner_id: str) -> None:
        """Apply already-durable boundary effects with the normal leased consumer."""
        from sose.composition import trading_company_customer as customer

        self.messages.append(message)
        effect = customer._consume_next(
            self.service,
            self.registry,
            owner_id=owner_id,
            now=message.produced_at,
            expected_message_id=message.message_id,
        )
        self.effects.append(effect)
        # Synchronous reference demonstration only. Production recurring
        # recovery uses a durable next tick, never wall-time busy-polling.
        deadline = customer.monotonic() + 12.0
        while True:
            try:
                customer._execute_intent(
                    self.persistence,
                    effect_id=effect,
                    fixtures=self.fixtures,
                    correlation_id=self.correlation_id,
                    organization_id=self.organization_id,
                )
                break
            except customer.DurableResourceWait:
                if customer.monotonic() >= deadline:
                    raise
                customer.sleep(0.02)

    def publish_consume_execute(
        self,
        *,
        contract_name: str,
        source_domain: str,
        source_identity: str,
        destination_domain: str,
        occurrence_key: str,
        owner_id: str,
        payload: dict[str, object],
        contract_version: int = 1,
    ) -> BoundaryMessage:
        from sose.composition import trading_company_customer as customer

        previous = self.messages[-1] if self.messages else None
        message = customer._publish(
            self.service,
            contract_name=contract_name,
            source_domain=source_domain,
            source_identity=source_identity,
            destination_domain=destination_domain,
            occurrence_key=occurrence_key,
            correlation_id=self.correlation_id,
            causation_id=None if previous is None else previous.message_id,
            produced_at=(
                self.origin
                if previous is None
                else customer._next_logical_time(
                    self.persistence, previous.produced_at,
                    organization_id=self.organization_id,
                )
            ),
            payload=payload,
            contract_version=contract_version,
        )
        self.consume_published_boundary(message, owner_id=owner_id)
        return message
