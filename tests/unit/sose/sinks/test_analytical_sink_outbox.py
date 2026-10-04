from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from sose.core.events import DomainEvent
from sose.jobs.model import SimulationJobState
from sose.persistence.memory import MemoryPersistence
from sose.sinks.base import SinkBinding
from sose.sinks.jsonl import JSONLAnalyticalSink
from sose.sinks.outbox import SinkOutbox


NOW = datetime(2026, 9, 28, 18, tzinfo=timezone.utc)


def _state() -> SimulationJobState:
    return SimulationJobState(
        job_id="job-1",
        domain_name="demo",
        config_json='{"start_at":"2026-09-28T18:00:00Z"}',
        config_revision=1,
        status="ready",
        initialized=True,
        logical_time=NOW,
        next_tick=1,
        run_count=1,
    )


def _append_event(persistence: MemoryPersistence, ordinal: int) -> DomainEvent:
    event = DomainEvent(
        event_id=f"event-{ordinal}",
        name="changed",
        entity_type="demo",
        entity_id="entity-1",
        occurred_at=NOW,
        tick=ordinal,
        payload={"ordinal": ordinal},
    )
    with persistence.transaction() as uow:
        uow.append_event(event)
    return event


def test_jsonl_outbox_delivery_is_durable_and_idempotent(tmp_path):
    persistence = MemoryPersistence()
    with persistence.transaction() as uow:
        uow.save_job_state(_state())
    _append_event(persistence, 1)

    sink = JSONLAnalyticalSink(tmp_path / "analytics.jsonl")
    binding = SinkBinding("warehouse", sink)
    outbox = SinkOutbox(persistence)

    delivered = outbox.flush(binding, _state())
    assert delivered is not None
    assert delivered.status == "delivered"
    assert delivered.attempts == 1

    checkpoint = persistence.sink_checkpoint("job-1", "warehouse")
    assert checkpoint is not None
    assert checkpoint.event_offset == 1

    # Re-flushing with no new events is a no-op and does not append again.
    assert outbox.flush(binding, _state()) is None
    lines = (tmp_path / "analytics.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["batch_id"] == delivered.batch.batch_id
    assert payload["events"][0]["event_id"] == "event-1"


def test_outbox_checkpoint_advances_only_after_successful_publish():
    class FailingSink:
        def publish(self, batch):
            raise RuntimeError("warehouse unavailable")

    persistence = MemoryPersistence()
    with persistence.transaction() as uow:
        uow.save_job_state(_state())
    _append_event(persistence, 1)

    outbox = SinkOutbox(persistence)
    binding = SinkBinding("warehouse", FailingSink())

    with pytest.raises(RuntimeError, match="warehouse unavailable"):
        outbox.flush(binding, _state())

    checkpoint = persistence.sink_checkpoint("job-1", "warehouse")
    assert checkpoint is None
    pending = outbox.pending(job_id="job-1", sink_name="warehouse")
    assert len(pending) == 1
    assert pending[0].attempts == 1
    assert pending[0].last_error == "RuntimeError: warehouse unavailable"


def test_failed_delivery_retries_same_batch_id_exactly_once_at_sink(tmp_path):
    class FailAfterPublish:
        def __init__(self, delegate):
            self.delegate = delegate
            self.failed = False

        def publish(self, batch):
            self.delegate.publish(batch)
            if not self.failed:
                self.failed = True
                raise RuntimeError("lost acknowledgement")

    persistence = MemoryPersistence()
    with persistence.transaction() as uow:
        uow.save_job_state(_state())
    _append_event(persistence, 1)

    jsonl = JSONLAnalyticalSink(tmp_path / "analytics.jsonl")
    binding = SinkBinding("warehouse", FailAfterPublish(jsonl))
    outbox = SinkOutbox(persistence)

    with pytest.raises(RuntimeError, match="lost acknowledgement"):
        outbox.flush(binding, _state())

    recovered = outbox.flush(binding, _state())
    assert recovered is not None and recovered.status == "delivered"
    assert recovered.attempts == 2

    lines = (tmp_path / "analytics.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_next_delivery_starts_at_previous_checkpoint():
    class CapturingSink:
        def __init__(self):
            self.batches = []

        def publish(self, batch):
            self.batches.append(batch)

    persistence = MemoryPersistence()
    with persistence.transaction() as uow:
        uow.save_job_state(_state())

    sink = CapturingSink()
    binding = SinkBinding("warehouse", sink)
    outbox = SinkOutbox(persistence)

    _append_event(persistence, 1)
    first = outbox.flush(binding, _state())
    assert first is not None
    _append_event(persistence, 2)
    second = outbox.flush(binding, _state())
    assert second is not None

    assert first.batch.from_event_offset == 0
    assert first.batch.to_event_offset == 1
    assert second.batch.from_event_offset == 1
    assert second.batch.to_event_offset == 2
    assert tuple(event.event_id for event in second.batch.events) == ("event-2",)


def test_jsonl_sink_ignores_blank_lines_when_checking_idempotency(tmp_path):
    persistence = MemoryPersistence()
    with persistence.transaction() as uow:
        uow.save_job_state(_state())
    _append_event(persistence, 1)

    path = tmp_path / "analytics.jsonl"
    sink = JSONLAnalyticalSink(path)
    path.write_text("\n\n", encoding="utf-8")

    delivery = SinkOutbox(persistence).flush(SinkBinding("warehouse", sink), _state())

    assert delivery is not None
    nonblank = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(nonblank) == 1
    assert json.loads(nonblank[0])["batch_id"] == delivery.batch.batch_id
