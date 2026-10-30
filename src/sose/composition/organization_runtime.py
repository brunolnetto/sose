from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from sose.composition.boundary import (
    BoundaryConsumerRegistry,
    BoundaryService,
    StaleBoundaryClaimError,
    UnsupportedBoundaryContractError,
)
from sose.composition.model import (
    BoundaryConsumption,
    BoundaryLease,
    BoundaryMessage,
)
from sose.composition.organization import (
    OrganizationDefinition,
    ProcessComponent,
    validate_organization_admission,
)
from sose.core.events import DomainEvent

if TYPE_CHECKING:
    from datetime import datetime, timedelta
    from sose.persistence.base import Persistence, UnitOfWork


OrganizationBoundaryHandler = Callable[[BoundaryMessage, "UnitOfWork"], str]


class OrganizationBoundaryConsumerRegistry:
    """Resolve admitted boundary handlers by organization, component and owner.

    The existing domain-level registry remains the audited contract catalogue.
    This registry adds the live instance binding that the v1 message format
    cannot express. It intentionally does not create StateCharts, jobs or a
    second durable source of business state.
    """

    def __init__(
        self,
        definition: OrganizationDefinition,
        *,
        catalog,
        manifests,
        contracts: BoundaryConsumerRegistry,
    ) -> None:
        self.definition = definition
        self.catalog = catalog
        self.contracts = contracts
        self.definition_fingerprint = validate_organization_admission(
            definition,
            catalog=catalog,
            manifests=manifests,
            consumers=contracts,
        )
        self._components: dict[str, ProcessComponent] = {
            item.component_id: item for item in definition.components
        }
        self._edges = {
            (
                edge.producer_id,
                edge.consumer_id,
                edge.contract_name,
                edge.contract_version,
            )
            for edge in definition.boundaries
        }
        self._handlers: dict[
            tuple[str, str, str, int], OrganizationBoundaryHandler
        ] = {}
        self._entity_types: dict[str, frozenset[str]] = {}
        for component in definition.components:
            domain = catalog.get(component.domain)
            self._entity_types[component.component_id] = frozenset(
                entry.name
                for entry in domain.registered_entity_types
                if getattr(entry.chart_factory, "__name__", "")
                in component.statechart_types
            )

    def contracts_for(self, destination_domain: str) -> frozenset[tuple[str, int]]:
        """Expose the audited domain contracts expected by static admission."""
        return self.contracts.contracts_for(destination_domain)

    def contracts_for_source(
        self, *, source_domain: str, destination_domain: str,
    ) -> frozenset[tuple[str, int]]:
        return self.contracts.contracts_for_source(
            source_domain=source_domain,
            destination_domain=destination_domain,
        )

    def register(
        self,
        *,
        producer_component_id: str,
        consumer_component_id: str,
        contract_name: str,
        contract_version: int,
        handler: OrganizationBoundaryHandler,
    ) -> None:
        key = (
            producer_component_id,
            consumer_component_id,
            contract_name,
            contract_version,
        )
        if key not in self._edges:
            raise ValueError("organization boundary route is not admitted")
        if not callable(handler):
            raise ValueError("organization boundary handler must be callable")
        if key in self._handlers:
            raise ValueError("organization boundary handler already registered")
        self._handlers[key] = handler

    def validate_message(self, message: BoundaryMessage) -> ProcessComponent:
        """Verify immutable organization/component/owner binding for a message."""
        if not isinstance(message, BoundaryMessage):
            raise TypeError("organization boundary requires BoundaryMessage")
        if not message.organization_scoped:
            raise ValueError("organization boundary requires a v2 scoped message")
        if (
            message.organization_id != self.definition.organization_id
            or message.definition_fingerprint != self.definition_fingerprint
            or message.config_epoch != self.definition.config_epoch
        ):
            raise ValueError("boundary message belongs to a different organization definition")

        source = self._components.get(message.source_component_id or "")
        destination = self._components.get(message.destination_component_id or "")
        if source is None or destination is None:
            raise ValueError("boundary message references an unknown component")
        if (
            message.source_domain != source.domain
            or message.source_owner_id != source.owner_id
            or message.destination_domain != destination.domain
            or message.destination_owner_id != destination.owner_id
        ):
            raise PermissionError("boundary message component owner/domain binding mismatch")

        route = (
            source.component_id,
            destination.component_id,
            message.contract_name,
            message.contract_version,
        )
        if route not in self._edges:
            raise ValueError("boundary message does not match an admitted component edge")
        if route not in self._handlers:
            raise UnsupportedBoundaryContractError(
                "organization component route has no registered handler"
            )
        return source

    def source_entity_types(self, component_id: str) -> frozenset[str]:
        try:
            return self._entity_types[component_id]
        except KeyError as exc:
            raise ValueError("unknown source component") from exc

    def resolve(self, message: BoundaryMessage) -> OrganizationBoundaryHandler:
        source = self.validate_message(message)
        key = (
            source.component_id,
            message.destination_component_id or "",
            message.contract_name,
            message.contract_version,
        )
        return self._handlers[key]


class OrganizationBoundaryService:
    """Owner-qualified façade over the existing durable boundary protocol.

    Claims are deliberately addressed by exact message ID. The generic v1
    claim scan is domain-scoped and cannot safely select one component owner's
    message when multiple organizations share a domain.
    """

    def __init__(
        self,
        persistence: "Persistence",
        registry: OrganizationBoundaryConsumerRegistry,
    ) -> None:
        self.persistence = persistence
        self.registry = registry
        self._protocol = BoundaryService(persistence)

    def publish(
        self,
        message: BoundaryMessage,
        *,
        recovery_job_id: str | None = None,
    ):
        source = self.registry.validate_message(message)
        if message.causation_kind not in {"event", "boundary"}:
            raise ValueError(
                "organization boundary requires typed event or boundary causation"
            )

        # Events and boundary publications are immutable append-only facts.
        # Validate their owner/component ancestry before delegating the
        # idempotent publisher; BoundaryService rechecks causal identity/time
        # in the transaction that commits publication and delivery.
        with self.persistence.transaction() as uow:
            if message.causation_kind == "event":
                cause = uow.get_event(message.causation_id or "")
                if cause is None:
                    raise ValueError("organization boundary source event is missing")
                if cause.owner_id != source.owner_id:
                    raise PermissionError("source event belongs to a different component owner")
                if cause.entity_type not in self.registry.source_entity_types(
                    source.component_id
                ):
                    raise PermissionError("source event entity type is not owned by source component")
                if cause.correlation_id != message.correlation_id:
                    raise ValueError("source event correlation contradicts boundary message")
                if cause.occurred_at > message.produced_at:
                    raise ValueError("boundary publication precedes its source event")
            else:
                parent = uow.get_boundary_message(message.causation_id or "")
                if parent is None:
                    raise ValueError("organization boundary predecessor is missing")
                self.registry.validate_message(parent)
                if (
                    parent.destination_component_id != message.source_component_id
                    or parent.destination_owner_id != message.source_owner_id
                ):
                    raise PermissionError(
                        "causal boundary predecessor does not terminate at source component owner"
                    )
                if parent.correlation_id != message.correlation_id:
                    raise ValueError("boundary predecessor correlation mismatch")
                if parent.produced_at > message.produced_at:
                    raise ValueError("boundary publication precedes its boundary predecessor")

        return self._protocol.publish(
            message,
            recovery_job_id=recovery_job_id,
        )

    def claim(
        self,
        *,
        message_id: str,
        owner_id: str,
        now: "datetime",
        lease_duration: "timedelta",
    ) -> BoundaryLease | None:
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("message_id must be a nonempty exact identity")
        with self.persistence.transaction() as uow:
            message = uow.get_boundary_message(message_id)
        if message is None:
            raise KeyError(f"unknown organization boundary message: {message_id}")
        self.registry.validate_message(message)
        return self._protocol.claim_next(
            owner_id=owner_id,
            now=now,
            lease_duration=lease_duration,
            destination_domain=message.destination_domain,
            accepted_contracts=frozenset({
                (message.contract_name, message.contract_version),
            }),
            message_id=message_id,
            correlation_id=message.correlation_id,
        )

    def consume(
        self,
        *,
        lease: BoundaryLease,
        now: "datetime",
    ) -> BoundaryConsumption:
        with self.persistence.transaction() as uow:
            message = uow.get_boundary_message(lease.message_id)
        if message is None:
            raise KeyError(f"unknown organization boundary message: {lease.message_id}")
        self.registry.validate_message(message)
        return self._protocol.consume(
            lease=lease,
            registry=self.registry,
            now=now,
        )
