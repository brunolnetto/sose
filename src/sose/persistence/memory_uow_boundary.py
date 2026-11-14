"""Job, boundary-protocol, effect, delivery and sink record methods of the in-memory unit of work.

Split from ``memory.py`` (``MemoryUnitOfWork``).
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from sose.composition.bindings import CustomerSettlementBinding
from sose.composition.effects import BusinessEffectApplied
from sose.composition.model import BoundaryConsumption, BoundaryDelivery, BoundaryMessage
from sose.domain.delivery import DomainDelivery
from sose.jobs.model import SimulationJobState
from sose.sinks.model import SinkCheckpoint, SinkDelivery

if TYPE_CHECKING:
    from .memory import _State


class BoundaryRecordsUnitOfWorkMixin:
    """Staged reads/writes of job, boundary, settlement, effect, delivery and sink records."""

    _working: _State

    def get_job_state(self, job_id: str) -> SimulationJobState | None:
        value = self._working.job_states.get(job_id)
        return deepcopy(value) if value else None

    def save_job_state(self, state: SimulationJobState) -> None:
        existing = self._working.job_states.get(state.job_id)
        if existing is not None and existing.domain_name != state.domain_name:
            raise ValueError(
                f"job domain cannot change: {state.job_id} "
                f"{existing.domain_name!r} -> {state.domain_name!r}"
            )
        self._working.job_states[state.job_id] = deepcopy(state)
        self._mark_dirty("job_states", state.job_id)

    def get_boundary_message(self, message_id: str) -> BoundaryMessage | None:
        value = self._working.boundary_messages.get(message_id)
        return deepcopy(value) if value else None

    def boundary_messages(self) -> tuple[BoundaryMessage, ...]:
        """Read all messages from the same authoritative UoW snapshot.

        Required for identifying unpaired/duplicated causal publications;
        looking up messages only through deliveries would hide orphan facts.
        """
        return tuple(
            deepcopy(self._working.boundary_messages[key])
            for key in sorted(self._working.boundary_messages)
        )

    def save_boundary_message(self, message: BoundaryMessage) -> None:
        existing = self._working.boundary_messages.get(message.message_id)
        if existing is not None and existing != message:
            raise ValueError(f"boundary message identity conflict: {message.message_id}")
        self._working.boundary_messages[message.message_id] = deepcopy(message)
        self._mark_dirty("boundary_messages", message.message_id)

    def get_boundary_delivery(self, delivery_id: str) -> BoundaryDelivery | None:
        value = self._working.boundary_deliveries.get(delivery_id)
        return deepcopy(value) if value else None

    def boundary_deliveries(self) -> tuple[BoundaryDelivery, ...]:
        return tuple(
            deepcopy(self._working.boundary_deliveries[key])
            for key in sorted(self._working.boundary_deliveries)
        )

    def save_boundary_delivery(self, delivery: BoundaryDelivery) -> None:
        existing = self._working.boundary_deliveries.get(delivery.delivery_id)
        if existing is not None and existing.message_id != delivery.message_id:
            raise ValueError(f"boundary delivery identity conflict: {delivery.delivery_id}")
        self._working.boundary_deliveries[delivery.delivery_id] = deepcopy(delivery)
        self._mark_dirty("boundary_deliveries", delivery.delivery_id)

    def get_boundary_consumption(self, delivery_id: str) -> BoundaryConsumption | None:
        value = self._working.boundary_consumptions.get(delivery_id)
        return deepcopy(value) if value else None

    def boundary_consumptions(self) -> tuple[BoundaryConsumption, ...]:
        """Inspect all ACK receipts from this transactional snapshot."""
        return tuple(
            deepcopy(self._working.boundary_consumptions[key])
            for key in sorted(self._working.boundary_consumptions)
        )

    def save_boundary_consumption(self, consumption: BoundaryConsumption) -> None:
        existing = self._working.boundary_consumptions.get(consumption.delivery_id)
        if existing is not None and existing != consumption:
            raise ValueError(f"boundary consumption identity conflict: {consumption.delivery_id}")
        self._working.boundary_consumptions[consumption.delivery_id] = deepcopy(consumption)
        self._mark_dirty("boundary_consumptions", consumption.delivery_id)

    def get_customer_settlement_binding(
        self,
        kind: str,
        entity_id: str,
    ) -> CustomerSettlementBinding | None:
        if kind not in {"order", "payment", "journal"}:
            raise ValueError("unknown settlement binding identity kind")
        value = self._working.customer_settlement_bindings.get((kind, entity_id))
        if value is None:
            return None
        expected = (
            ("order", value.order_id),
            ("payment", value.payment_id),
            ("journal", value.journal_id),
        )
        if any(self._working.customer_settlement_bindings.get(key) != value for key in expected):
            raise RuntimeError("incomplete settlement binding identity indices")
        return deepcopy(value)

    def save_customer_settlement_binding(self, binding: CustomerSettlementBinding) -> None:
        # Three independent lookup keys are committed together in one UoW.
        # Assignment of any order, payment or journal ID is immutable. The
        # public service also takes the shared PostgreSQL boundary lock.
        indices = (
            ("order", binding.order_id),
            ("payment", binding.payment_id),
            ("journal", binding.journal_id),
        )
        previously = [self._working.customer_settlement_bindings.get(key) for key in indices]
        for key, existing in zip(indices, previously):
            if existing is not None and existing != binding:
                raise ValueError(f"settlement identity already bound: {key}")
        if any(existing is not None for existing in previously) and not all(
            existing is not None for existing in previously
        ):
            raise RuntimeError("incomplete settlement binding identity indices")
        for key in indices:
            self._working.customer_settlement_bindings[key] = deepcopy(binding)
            self._mark_dirty("customer_settlement_bindings", key)

    def get_business_effect(self, effect_id: str) -> BusinessEffectApplied | None:
        result = self._working.business_effects.get(effect_id)
        return deepcopy(result) if result else None

    def business_effects(self) -> tuple[BusinessEffectApplied, ...]:
        """Enumerate all authoritative certificates in this same UoW snapshot."""
        return tuple(
            deepcopy(self._working.business_effects[key])
            for key in sorted(self._working.business_effects)
        )

    def save_business_effect(self, effect: BusinessEffectApplied) -> None:
        previous = self._working.business_effects.get(effect.effect_id)
        if previous is not None and previous != effect:
            raise ValueError(f"business-effect certificate identity conflicts: {effect.effect_id}")
        self._working.business_effects[effect.effect_id] = deepcopy(effect)
        self._mark_dirty("business_effects", effect.effect_id)

    def get_domain_delivery(self, mutation_id: str) -> DomainDelivery | None:
        value = self._working.domain_deliveries.get(mutation_id)
        return deepcopy(value) if value else None

    def save_domain_delivery(self, delivery: DomainDelivery) -> None:
        existing = self._working.domain_deliveries.get(delivery.mutation_id)
        if existing is not None and existing.mutation != delivery.mutation:
            raise ValueError(f"domain mutation identity conflict: {delivery.mutation_id}")
        self._working.domain_deliveries[delivery.mutation_id] = deepcopy(delivery)
        self._mark_dirty("domain_deliveries", delivery.mutation_id)

    def delete_domain_delivery(self, mutation_id: str) -> None:
        self._working.domain_deliveries.pop(mutation_id, None)
        self._mark_dirty("domain_deliveries", mutation_id)

    def get_sink_delivery(self, delivery_id: str) -> SinkDelivery | None:
        value = self._working.sink_deliveries.get(delivery_id)
        return deepcopy(value) if value else None

    def save_sink_delivery(self, delivery: SinkDelivery) -> None:
        self._working.sink_deliveries[delivery.delivery_id] = deepcopy(delivery)
        self._mark_dirty("sink_deliveries", delivery.delivery_id)

    def delete_sink_delivery(self, delivery_id: str) -> None:
        self._working.sink_deliveries.pop(delivery_id, None)
        self._mark_dirty("sink_deliveries", delivery_id)

    def get_sink_checkpoint(
        self,
        job_id: str,
        sink_name: str,
    ) -> SinkCheckpoint | None:
        value = self._working.sink_checkpoints.get((job_id, sink_name))
        return deepcopy(value) if value else None

    def save_sink_checkpoint(self, checkpoint: SinkCheckpoint) -> None:
        key = (checkpoint.job_id, checkpoint.sink_name)
        self._working.sink_checkpoints[key] = deepcopy(checkpoint)
        self._mark_dirty("sink_checkpoints", key)
