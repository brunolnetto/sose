from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from sose.core.events import DomainEvent
from sose.jobs.model import SimulationJobState
from sose.persistence.memory import MemoryPersistence
from sose.sinks.base import SinkBinding
from sose.sinks.model import AnalyticalBatch, SinkCheckpoint, SinkDelivery
from sose.sinks.outbox import SinkOutbox


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _state() -> SimulationJobState:
    return SimulationJobState(
        job_id="job",
        domain_name="domain",
        config_json="{}",
        config_revision=1,
        status="ready",
        initialized=True,
        logical_time=NOW,
        next_tick=1,
    )


def _event(event_id="e1") -> DomainEvent:
    return DomainEvent(
        event_id=event_id,
        name="created",
        entity_type="order",
        entity_id="1",
        occurred_at=NOW,
    )


def _batch(*, start=0, end=1) -> AnalyticalBatch:
    events = tuple(_event(f"e{i}") for i in range(start, end))
    return AnalyticalBatch(
        batch_id=f"batch-{start}-{end}",
        job_id="job",
        domain_name="domain",
        config_revision=1,
        logical_tick=1,
        logical_time=NOW,
        from_event_offset=start,
        to_event_offset=end,
        events=events,
    )


def _delivery(*, start=0, end=1, status="pending") -> SinkDelivery:
    return SinkDelivery(
        delivery_id=f"delivery-{start}-{end}",
        sink_name="sink",
        batch=_batch(start=start, end=end),
        status=status,
        delivered_at=NOW if status == "delivered" else None,
    )


class _Sink:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.batches = []

    def publish(self, batch):
        self.batches.append(batch)
        if self.error is not None:
            raise self.error


def test_sink_outbox_prepare_none_pending_existing_and_new_delivery():
    persistence = MemoryPersistence()
    outbox = SinkOutbox(persistence)
    state = _state()

    assert outbox.prepare(state, sink_name="sink") is None

    with persistence.transaction() as uow:
        uow.append_event(_event())

    prepared = outbox.prepare(state, sink_name="sink")
    assert prepared is not None
    assert prepared.status == "pending"
    assert outbox.pending(job_id="job", sink_name="sink") == (prepared,)

    # A second prepare must surface the durable pending delivery rather than
    # manufacturing another batch.
    assert outbox.prepare(state, sink_name="sink") == prepared


class _PrepareRaceUow:
    def __init__(self, checkpoint=None, existing=None):
        self.checkpoint = checkpoint
        self.existing = existing
        self.saved = []

    def get_sink_checkpoint(self, job_id, sink_name):
        return self.checkpoint

    def get_sink_delivery(self, delivery_id):
        if self.existing is None or self.existing.delivery_id != delivery_id:
            return None
        return self.existing

    def save_sink_delivery(self, delivery):
        self.saved.append(delivery)


class _PrepareRacePersistence:
    def __init__(self, *, checkpoint=None, uow=None, events=None, pending=()):
        self._checkpoint = checkpoint
        self._uow = uow or _PrepareRaceUow()
        self._events = tuple(events or ())
        self._pending = tuple(pending)

    def sink_deliveries(self, **kwargs):
        return self._pending

    def sink_checkpoint(self, job_id, sink_name):
        return self._checkpoint

    def events(self):
        return self._events

    @contextmanager
    def transaction(self):
        yield self._uow


def test_prepare_rejects_stale_checkpoint_race():
    initial = SinkCheckpoint("job", "sink", event_offset=0)
    advanced = SinkCheckpoint("job", "sink", event_offset=1)
    uow = _PrepareRaceUow(checkpoint=advanced)
    persistence = _PrepareRacePersistence(
        checkpoint=initial,
        uow=uow,
        events=(_event(),),
    )

    assert SinkOutbox(persistence).prepare(_state(), sink_name="sink") is None
    assert uow.saved == []


def test_prepare_returns_existing_deterministic_delivery():
    existing = _delivery()
    uow = _PrepareRaceUow(existing=existing)
    persistence = _PrepareRacePersistence(
        checkpoint=SinkCheckpoint("job", "sink", event_offset=0),
        uow=uow,
        events=(_event(),),
    )

    result = SinkOutbox(persistence).prepare(_state(), sink_name="sink")
    assert result is not None
    assert result != existing
    assert uow.saved == [result]

    # The second prepare sees the exact deterministic identity persisted by the
    # first attempt and must return it rather than manufacturing another record.
    uow.existing = result
    result2 = SinkOutbox(persistence).prepare(_state(), sink_name="sink")
    assert result2 == result
    assert uow.saved == [result]


def test_deliver_validates_binding_and_short_circuits_completed():
    persistence = MemoryPersistence()
    outbox = SinkOutbox(persistence)
    pending = _delivery()
    sink = _Sink()

    with pytest.raises(ValueError, match="does not own delivery"):
        outbox.deliver(SinkBinding("other", sink), pending)

    completed = replace(pending, status="delivered", delivered_at=NOW)
    assert outbox.deliver(SinkBinding("sink", sink), completed) is completed
    assert sink.batches == []


def test_delivery_failure_is_recorded_before_reraising():
    persistence = MemoryPersistence()
    delivery = _delivery()
    with persistence.transaction() as uow:
        uow.save_sink_delivery(delivery)

    outbox = SinkOutbox(persistence)
    sink = _Sink(RuntimeError("warehouse down"))

    with pytest.raises(RuntimeError, match="warehouse down"):
        outbox.deliver(SinkBinding("sink", sink), delivery)

    failed = persistence.sink_deliveries(job_id="job", sink_name="sink")[0]
    assert failed.attempts == 1
    assert failed.last_error == "RuntimeError: warehouse down"
    assert failed.status == "pending"


def test_delivery_failure_does_not_overwrite_already_delivered_race():
    delivered = replace(_delivery(), status="delivered", delivered_at=NOW)

    class Uow:
        def get_sink_delivery(self, delivery_id):
            return delivered

        def save_sink_delivery(self, delivery):
            raise AssertionError("already delivered state must win")

    class Persistence:
        @contextmanager
        def transaction(self):
            yield Uow()

    with pytest.raises(RuntimeError, match="down"):
        SinkOutbox(Persistence()).deliver(
            SinkBinding("sink", _Sink(RuntimeError("down"))),
            _delivery(),
        )


def test_successful_delivery_commits_checkpoint_and_removes_pending():
    persistence = MemoryPersistence()
    delivery = _delivery()
    with persistence.transaction() as uow:
        uow.save_sink_delivery(delivery)

    sink = _Sink()
    completed = SinkOutbox(persistence).deliver(SinkBinding("sink", sink), delivery)

    assert completed.status == "delivered"
    assert completed.attempts == 1
    assert completed.delivered_at == NOW
    assert persistence.sink_deliveries(job_id="job", sink_name="sink") == ()
    checkpoint = persistence.sink_checkpoint("job", "sink")
    assert checkpoint is not None
    assert checkpoint.event_offset == 1
    assert checkpoint.last_delivery_id == delivery.delivery_id


def test_successful_delivery_returns_concurrent_completed_record():
    existing = replace(_delivery(), status="delivered", attempts=2, delivered_at=NOW)

    class Uow:
        def get_sink_delivery(self, delivery_id):
            return existing

    class Persistence:
        @contextmanager
        def transaction(self):
            yield Uow()

    result = SinkOutbox(Persistence()).deliver(
        SinkBinding("sink", _Sink()),
        _delivery(),
    )
    assert result is existing


class _CheckpointUow:
    def __init__(self, checkpoint):
        self.checkpoint = checkpoint
        self.saved = None
        self.deleted = None

    def get_sink_delivery(self, delivery_id):
        return None

    def get_sink_checkpoint(self, job_id, sink_name):
        return self.checkpoint

    def save_sink_checkpoint(self, checkpoint):
        self.saved = checkpoint

    def delete_sink_delivery(self, delivery_id):
        self.deleted = delivery_id


class _CheckpointPersistence:
    def __init__(self, checkpoint):
        self.uow = _CheckpointUow(checkpoint)

    @contextmanager
    def transaction(self):
        yield self.uow


def test_delivery_preserves_later_checkpoint():
    later = SinkCheckpoint("job", "sink", event_offset=3, last_delivery_id="later")
    persistence = _CheckpointPersistence(later)
    delivery = _delivery(start=1, end=2)

    completed = SinkOutbox(persistence).deliver(
        SinkBinding("sink", _Sink()),
        delivery,
    )

    assert completed.status == "delivered"
    assert persistence.uow.saved is later
    assert persistence.uow.deleted == delivery.delivery_id


def test_delivery_rejects_gap_behind_start():
    persistence = _CheckpointPersistence(
        SinkCheckpoint("job", "sink", event_offset=0)
    )
    delivery = _delivery(start=1, end=2)

    with pytest.raises(RuntimeError, match="earlier delivery is unresolved"):
        SinkOutbox(persistence).deliver(
            SinkBinding("sink", _Sink()),
            delivery,
        )


def test_flush_covers_empty_race_and_pending_race(monkeypatch):
    persistence = MemoryPersistence()
    outbox = SinkOutbox(persistence)
    binding = SinkBinding("sink", _Sink())
    state = _state()

    assert outbox.flush(binding, state) is None

    pending = _delivery()
    with persistence.transaction() as uow:
        uow.save_sink_delivery(pending)

    # Model prepare losing an optimistic race after another worker made the
    # delivery durable. flush() must reread pending and deliver it.
    monkeypatch.setattr(outbox, "prepare", lambda state, sink_name: None)
    completed = outbox.flush(binding, state)
    assert completed is not None
    assert completed.status == "delivered"
