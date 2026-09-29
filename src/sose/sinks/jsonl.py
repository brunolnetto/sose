from __future__ import annotations

import json
import os
from pathlib import Path

from sose.persistence.codec import dumps

from .model import AnalyticalBatch


class JSONLAnalyticalSink:
    """Idempotent append-only analytical sink used as the reference adapter."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def _contains(self, batch_id: str) -> bool:
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("batch_id") == batch_id:
                    return True
        return False

    def publish(self, batch: AnalyticalBatch) -> None:
        if self._contains(batch.batch_id):
            return

        record = {
            "batch_id": batch.batch_id,
            "job_id": batch.job_id,
            "domain_name": batch.domain_name,
            "config_revision": batch.config_revision,
            "logical_tick": batch.logical_tick,
            "logical_time": batch.logical_time.isoformat(),
            "from_event_offset": batch.from_event_offset,
            "to_event_offset": batch.to_event_offset,
            "events": [
                {
                    "event_id": event.event_id,
                    "name": event.name,
                    "entity_type": event.entity_type,
                    "entity_id": event.entity_id,
                    "occurred_at": event.occurred_at.isoformat(),
                    "tick": event.tick,
                    "payload": dumps(dict(event.payload)),
                    "causation_id": event.causation_id,
                    "correlation_id": event.correlation_id,
                }
                for event in batch.events
            ],
        }
        payload = json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
