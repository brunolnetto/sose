"""#496 W4a: bounded executable organization boundary and independent real jobs.

Uses the existing SimulationJob, BoundaryService and BusinessEffectService.
It is deliberately single-store for boundary transactions: multi-store owner
fencing, shared physical assets and full organization runtime await #496/#477.
Neither an ACK nor a declared owner is itself a business effect certificate.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Mapping

from sose.composition.boundary import BoundaryConsumerRegistry, BoundaryService
from sose.composition.effects import BusinessEffectService
from sose.composition.model import BoundaryMessage, BoundaryDelivery, DeliveryStatus
from sose.composition.organization import OrganizationDefinition, validate_organization_admission
from sose.jobs.runner import SimulationJob


class AdmittedOrganizationExecutor:
    """Explicitly admitted component jobs and owner-fenced causal ingress.

    Component.owner_id MUST be the real SimulationJob.job_id because SOSE's
    reference-domain Engine and durable clocks use exactly that owner ID.
    The boundary still uses the original single-store scoped UoW/claim fence.
    """

    def __init__(self, *, definition: OrganizationDefinition, persistence,
                 jobs: Mapping[str, SimulationJob], catalog, manifests,
                 consumers: BoundaryConsumerRegistry) -> None:
        self.fingerprint = validate_organization_admission(
            definition, catalog=catalog, manifests=manifests, consumers=consumers,
        )
        self.definition = definition
        self.persistence = persistence
        self.consumers = consumers
        self._components = {c.component_id: c for c in definition.components}
        self._scheduled = {j.job_id: j for j in definition.jobs}
        self._jobs = dict(jobs)
        if set(self._jobs) != set(self._scheduled):
            raise ValueError("executable organization requires exact declared job mapping")
        for job_id, job in self._jobs.items():
            scheduled = self._scheduled[job_id]
            component = self._components[scheduled.component_id]
            if not isinstance(job, SimulationJob):
                raise TypeError("component runner must be a real SimulationJob")
            if (
                job.job_id != job_id
                or component.owner_id != job_id
                or job.definition.name != component.domain
                or job.persistence is not persistence
            ):
                raise ValueError("component runner owner/domain/store contradicts admitted topology")
        self._boundary = BoundaryService(persistence)
        self._business = BusinessEffectService(persistence)

    def run_job(self, *, job_id: str, trigger_id: str, ticks: int = 1,
                recover: bool = False):
        """Advance ONLY the chosen registered job and its owner-local clock."""
        scheduled = self._scheduled.get(job_id)
        if scheduled is None:
            raise ValueError("undeclared organization job")
        if not isinstance(trigger_id, str) or not trigger_id.startswith(
            scheduled.trigger_namespace + ":"
        ):
            raise ValueError("component trigger must have its admitted namespace")
        return self._jobs[job_id].run_trigger(
            trigger_id=trigger_id, ticks=ticks, recover=recover,
        )

    def _admitted_edge(self, producer_id: str, consumer_id: str,
                       contract_name: str, version: int):
        matches = [
            edge for edge in self.definition.boundaries
            if (edge.producer_id, edge.consumer_id, edge.contract_name,
                edge.contract_version) == (
                    producer_id, consumer_id, contract_name, version,
                )
        ]
        if len(matches) != 1:
            raise ValueError("boundary not uniquely admitted for component pair")
        return matches[0]

    def _source(self, uow, message: BoundaryMessage, producer_id: str):
        producer = self._components[producer_id]
        if message.causation_kind != "event" or not message.causation_id:
            raise ValueError("executable boundary requires typed durable DomainEvent cause")
        event = uow.get_event(message.causation_id)
        if event is None:
            raise ValueError("executable boundary source DomainEvent missing")
        registered = frozenset(
            entry.name for entry in self._jobs_by_component(producer_id).definition.registered_entity_types
            if entry.chart_factory.__name__ in producer.statechart_types
        )
        if (
            event.owner_id != producer.owner_id
            or event.entity_type not in registered
            or event.name != "entity.state_transition"
            or event.entity_id != message.source_identity
            or event.correlation_id != message.correlation_id
            or event.occurred_at > message.produced_at
            or not all(
                isinstance(event.payload.get(field), str)
                and event.payload[field].strip()
                for field in ("trigger", "from_state", "to_state")
            )
        ):
            raise ValueError("executable boundary source owner/StateChart/causality mismatch")
        return event

    def _jobs_by_component(self, component_id: str) -> SimulationJob:
        scheduled = [j for j in self._scheduled.values() if j.component_id == component_id]
        if len(scheduled) != 1:
            raise ValueError("bounded causal executor requires one real job per active component")
        return self._jobs[scheduled[0].job_id]

    def _check_message(self, uow, message: BoundaryMessage,
                       producer_id: str, consumer_id: str):
        self._admitted_edge(
            producer_id, consumer_id, message.contract_name, message.contract_version,
        )
        source = self._components[producer_id]
        target = self._components[consumer_id]
        if (message.source_domain != source.domain
                or message.destination_domain != target.domain):
            raise ValueError("executable boundary source/destination domain mismatch")
        return self._source(uow, message, producer_id)

    def publish(self, *, producer_id: str, consumer_id: str,
                source_event_id: str, contract_name: str, contract_version: int,
                occurrence_key: str, produced_at: datetime, payload: dict):
        """Publish a typed message ONLY from an already persisted owned event."""
        edge = self._admitted_edge(
            producer_id, consumer_id, contract_name, contract_version,
        )
        producer = self._components[edge.producer_id]
        consumer = self._components[edge.consumer_id]
        with self.persistence.transaction() as uow:
            event = uow.get_event(source_event_id)
            if event is None:
                raise ValueError("executable boundary source DomainEvent missing")
            message = BoundaryMessage.create(
                contract_name=contract_name, contract_version=contract_version,
                source_domain=producer.domain, source_identity=event.entity_id,
                destination_domain=consumer.domain, occurrence_key=occurrence_key,
                correlation_id=event.correlation_id,
                causation_id=source_event_id, causation_kind="event",
                produced_at=produced_at, payload=payload,
            )
            self._check_message(uow, message, producer_id, consumer_id)
        # BoundaryService rechecks typed durable causation under its own UoW.
        return message, self._boundary.publish(message)

    def consume(self, *, producer_id: str, consumer_id: str,
                message_id: str, now: datetime, lease_duration: timedelta):
        """Claim and stage an exact owner-qualified Command in the ACK UoW.

        The installed domain-specific handler remains the execution strategy,
        but may not return an arbitrary opaque effect ID without durably
        staging a matching Command owned by the intended consumer.
        """
        consumer_job = self._jobs_by_component(consumer_id)
        consumer = self._components[consumer_id]
        with self.persistence.transaction() as uow:
            message = uow.get_boundary_message(message_id)
            if message is None:
                raise ValueError("executable boundary message missing")
            self._check_message(uow, message, producer_id, consumer_id)
            existing_delivery = uow.get_boundary_delivery(
                BoundaryDelivery.pending(message).delivery_id
            )
            if existing_delivery is None:
                raise ValueError("executable boundary lacks durable delivery")
            if existing_delivery.status is DeliveryStatus.CONSUMED:
                existing = uow.get_boundary_consumption(existing_delivery.delivery_id)
                if existing is None:
                    raise ValueError("consumed boundary lacks durable ACK")
                command = uow.get_command(existing.consumer_effect_id)
                certificate = uow.get_business_effect(existing.consumer_effect_id)
                if command is None and certificate is None:
                    raise ValueError("consumed boundary lacks Command or certificate")
                if command is not None:
                    self._check_command(command, message, consumer)
                if certificate is not None and (
                    certificate.effect_id != existing.consumer_effect_id
                    or certificate.boundary_message_id != message.message_id
                    or certificate.correlation_id != message.correlation_id
                ):
                    raise ValueError("replayed boundary certificate contradicts ACK")
                return existing

        # This is a per-message claim. A worker is not given permission to
        # drain an unrelated organization's same-domain deliveries.
        lease = self._boundary.claim_next(
            owner_id=consumer_job.job_id, now=now, lease_duration=lease_duration,
            message_id=message_id, destination_domain=consumer.domain,
            accepted_contracts=frozenset({
                (message.contract_name, message.contract_version),
            }), correlation_id=message.correlation_id,
        )
        if lease is None:
            raise RuntimeError("executable boundary not claimable (wait or stale predecessor)")
        registry = BoundaryConsumerRegistry()

        def stage(message: BoundaryMessage, uow):
            self._check_message(uow, message, producer_id, consumer_id)
            effect_id = self.consumers.resolve(message)(message, uow)
            command = uow.get_command(effect_id)
            if command is None:
                raise ValueError("boundary ACK lacks durable staged Command")
            self._check_command(command, message, consumer)
            return effect_id

        registry.register(
            source_domain=message.source_domain,
            destination_domain=message.destination_domain,
            contract_name=message.contract_name,
            contract_version=message.contract_version,
            handler=stage,
        )
        return self._boundary.consume(lease=lease, registry=registry, now=now)

    @staticmethod
    def _check_command(command, message, consumer):
        if (
            command.command_id == "" or command.owner_id != consumer.owner_id
            or command.causation_id != message.message_id
            or command.correlation_id != message.correlation_id
            or not command.name.startswith("composition.")
        ):
            raise ValueError("staged Command owner or causal boundary contradicts component")

    def certify(self, *, producer_id: str, consumer_id: str,
                message_id: str, effect_id: str, completed_at: datetime):
        """Use the existing domain-terminal oracle, NEVER certify ACK alone."""
        with self.persistence.transaction() as uow:
            msg = uow.get_boundary_message(message_id)
            if msg is None:
                raise ValueError("certification lacks durable boundary message")
            self._check_message(uow, msg, producer_id, consumer_id)
            delivery = uow.get_boundary_delivery(BoundaryDelivery.pending(msg).delivery_id)
            if delivery is None or delivery.status is not DeliveryStatus.CONSUMED:
                raise ValueError("certification requires consumed durable delivery")
            if delivery.consumer_effect_id != effect_id:
                raise ValueError("certification effect identity contradicts ACK")
            pending = uow.get_command(effect_id)
            certificate = uow.get_business_effect(effect_id)
            if pending is not None:
                self._check_command(pending, msg, self._components[consumer_id])
                # A terminal projection alone is not sufficient: it may have
                # been reached BEFORE this message was ever ACKed. Anchor a
                # real terminal StateChart event in the consumer owner's own
                # durable history at/after the ACK before asking the existing
                # certified-terminal oracle to finalize the effect.
                from sose.composition.effects import _CERTIFIED_TERMINALS
                terminal = _CERTIFIED_TERMINALS.get(pending.name)
                if terminal is None:
                    raise ValueError("staged Command has no certified terminal")
                kind, state = terminal[:2]
                if not any(
                    event.owner_id == self._components[consumer_id].owner_id
                    and event.name == "entity.state_transition"
                    and event.entity_type == kind
                    and event.entity_id == pending.entity_id
                    and event.payload.get("to_state") == state
                    and delivery.consumed_at is not None
                    and event.occurred_at >= delivery.consumed_at
                    and completed_at >= event.occurred_at
                    for event in uow.events()
                ):
                    raise ValueError("certified terminal transition not attested after ACK")
            elif certificate is None:
                raise ValueError("certification requires a staged Command or existing certificate")
        return self._business.complete(effect_id=effect_id, completed_at=completed_at)
