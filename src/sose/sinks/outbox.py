from __future__ import annotations

from dataclasses import replace

from sose.core.identity import deterministic_id
from sose.jobs.model import SimulationJobState
from sose.persistence.base import Persistence

from .base import SinkBinding
from .model import AnalyticalBatch, SinkCheckpoint, SinkDelivery


class SinkOutbox:
    """Durable event-delivery outbox for analytical sinks.

    Operational persistence is authoritative. A sink failure never rolls back a
    committed simulation tick. Delivery can be retried by deterministic
    delivery_id; concrete sinks must publish idempotently by batch_id.
    """

    def __init__(self, persistence: Persistence) -> None:
        self.persistence = persistence

    def pending(
        self,
        *,
        job_id: str,
        sink_name: str,
    ) -> tuple[SinkDelivery, ...]:
        return tuple(
            delivery
            for delivery in self.persistence.sink_deliveries(
                job_id=job_id,
                sink_name=sink_name,
            )
            if delivery.status == "pending"
        )

    def prepare(
        self,
        state: SimulationJobState,
        *,
        sink_name: str,
    ) -> SinkDelivery | None:
        pending = self.pending(job_id=state.job_id, sink_name=sink_name)
        if pending:
            return pending[0]

        checkpoint = self.persistence.sink_checkpoint(
            state.job_id,
            sink_name,
        ) or SinkCheckpoint(
            job_id=state.job_id,
            sink_name=sink_name,
        )
        events = self.persistence.events()
        if checkpoint.event_offset >= len(events):
            return None

        selected = tuple(events[checkpoint.event_offset :])
        to_offset = len(events)
        last_event_id = selected[-1].event_id if selected else ""
        batch_id = deterministic_id(
            "analytical-batch",
            state.job_id,
            sink_name,
            checkpoint.event_offset,
            to_offset,
            last_event_id,
        )
        batch = AnalyticalBatch(
            batch_id=batch_id,
            job_id=state.job_id,
            domain_name=state.domain_name,
            config_revision=state.config_revision,
            logical_tick=state.next_tick,
            logical_time=state.logical_time,
            from_event_offset=checkpoint.event_offset,
            to_event_offset=to_offset,
            events=selected,
        )
        delivery = SinkDelivery(
            delivery_id=deterministic_id(
                "sink-delivery",
                sink_name,
                batch_id,
            ),
            sink_name=sink_name,
            batch=batch,
        )

        with self.persistence.transaction() as uow:
            current_checkpoint = uow.get_sink_checkpoint(
                state.job_id,
                sink_name,
            )
            current_offset = (
                0
                if current_checkpoint is None
                else current_checkpoint.event_offset
            )
            if current_offset != checkpoint.event_offset:
                # Another worker progressed this sink. Re-evaluate against the
                # authoritative checkpoint rather than persisting an overlapping
                # delivery.
                return None

            existing = uow.get_sink_delivery(delivery.delivery_id)
            if existing is not None:
                return existing
            uow.save_sink_delivery(delivery)
        return delivery

    def deliver(
        self,
        binding: SinkBinding,
        delivery: SinkDelivery,
    ) -> SinkDelivery:
        if delivery.sink_name != binding.name:
            raise ValueError("sink binding does not own delivery")
        if delivery.status == "delivered":
            return delivery

        try:
            binding.sink.publish(delivery.batch)
        except Exception as exc:
            self._record_failed_delivery(delivery, exc)
            raise

        completed = self._completed_delivery(delivery)
        checkpoint = self._delivery_checkpoint(binding, delivery)
        with self.persistence.transaction() as uow:
            current = uow.get_sink_delivery(delivery.delivery_id)
            if current is not None and current.status == "delivered":
                return current

            existing_checkpoint = uow.get_sink_checkpoint(
                delivery.batch.job_id,
                binding.name,
            )
            existing_offset = (
                0
                if existing_checkpoint is None
                else existing_checkpoint.event_offset
            )
            if existing_offset > delivery.batch.from_event_offset:
                # An equal/later batch already committed the cursor. Preserve the
                # monotonic checkpoint and only mark this retry delivered.
                checkpoint = existing_checkpoint
            elif existing_offset < delivery.batch.from_event_offset:
                raise RuntimeError(
                    "sink checkpoint is behind delivery start; "
                    "an earlier delivery is unresolved"
                )

            uow.save_sink_checkpoint(checkpoint)
            uow.delete_sink_delivery(delivery.delivery_id)
        return completed

    def _record_failed_delivery(self, delivery: SinkDelivery, exc: Exception) -> None:
        failed = replace(
            delivery,
            attempts=delivery.attempts + 1,
            last_error=f"{type(exc).__name__}: {exc}",
        )
        with self.persistence.transaction() as uow:
            current = uow.get_sink_delivery(delivery.delivery_id)
            if current is not None and current.status != "delivered":
                uow.save_sink_delivery(failed)

    @staticmethod
    def _completed_delivery(delivery: SinkDelivery) -> SinkDelivery:
        return replace(
            delivery,
            status="delivered",
            attempts=delivery.attempts + 1,
            last_error=None,
            delivered_at=delivery.batch.logical_time,
        )

    @staticmethod
    def _delivery_checkpoint(
        binding: SinkBinding,
        delivery: SinkDelivery,
    ) -> SinkCheckpoint:
        return SinkCheckpoint(
            job_id=delivery.batch.job_id,
            sink_name=binding.name,
            event_offset=delivery.batch.to_event_offset,
            last_delivery_id=delivery.delivery_id,
        )

    def flush(
        self,
        binding: SinkBinding,
        state: SimulationJobState,
    ) -> SinkDelivery | None:
        delivery = self.prepare(state, sink_name=binding.name)
        if delivery is None:
            # prepare() can lose an optimistic race with another flusher. Read
            # once more to surface any durable pending delivery.
            pending = self.pending(job_id=state.job_id, sink_name=binding.name)
            if not pending:
                return None
            delivery = pending[0]
        return self.deliver(binding, delivery)
