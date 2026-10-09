from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Iterator

from .memory import MemoryPersistence, MemoryUnitOfWork, _State, fork_state
from .records import (
    StateRecord,
    StateRecordChange,
    changes_for_dirty_records,
    records_to_state,
)


_SCHEMA_VERSION = 1


class JSONLJournalPersistence(MemoryPersistence):
    """Single-writer append-only durable transaction journal.

    Each committed transaction is one JSON line containing record-level deltas.
    A malformed/truncated final line is ignored during replay, so only complete
    transaction records become durable semantic truth.
    """

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
        self._transaction_count = 0
        self._refresh_from_journal()

    @staticmethod
    def _is_truncated_final_line(handle) -> bool:
        remaining = handle.read().strip()
        return not remaining

    @staticmethod
    def _journal_change(raw: dict[str, object]) -> StateRecordChange:
        return StateRecordChange(
            operation=str(raw["operation"]),
            collection=str(raw["collection"]),
            key=str(raw["key"]),
            position=(
                None
                if raw.get("position") is None
                else int(raw["position"])
            ),
            payload=raw.get("payload"),
        )

    @staticmethod
    def _apply_change(
        records: dict[tuple[str, str], StateRecord],
        change: StateRecordChange,
    ) -> None:
        identity = (change.collection, change.key)
        if change.operation == "delete":
            records.pop(identity, None)
            return
        if change.operation != "upsert":
            raise RuntimeError(
                "unknown JSONL journal operation: "
                f"{change.operation}"
            )
        if change.position is None or change.payload is None:
            raise RuntimeError(
                "invalid JSONL journal upsert without payload"
            )
        records[identity] = StateRecord(
            collection=change.collection,
            key=change.key,
            position=change.position,
            payload=str(change.payload),
        )

    def _refresh_from_journal(self) -> None:
        records: dict[tuple[str, str], StateRecord] = {}
        transaction_count = 0

        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    # A process crash can leave one partial final append. Ignore
                    # only that final fragment; corruption in the middle is fatal.
                    if not self._is_truncated_final_line(handle):
                        raise RuntimeError(
                            f"corrupt JSONL journal at line {line_number}"
                        )
                    break

                if int(entry.get("schema_version", -1)) != _SCHEMA_VERSION:
                    raise RuntimeError(
                        "unsupported JSONLJournalPersistence schema version: "
                        f"{entry.get('schema_version')}"
                    )
                transaction_count = max(
                    transaction_count,
                    int(entry["transaction"]),
                )
                for raw in entry["changes"]:
                    change = self._journal_change(raw)
                    self._apply_change(records, change)

        self._transaction_count = transaction_count
        self._state = records_to_state(records.values())

    def _append_transaction(
        self,
        changes: tuple[StateRecordChange, ...],
    ) -> None:
        if not changes:
            return

        transaction = self._transaction_count + 1
        entry = {
            "schema_version": _SCHEMA_VERSION,
            "transaction": transaction,
            "changes": [
                {
                    "operation": change.operation,
                    "collection": change.collection,
                    "key": change.key,
                    "position": change.position,
                    "payload": change.payload,
                }
                for change in changes
            ],
        }
        payload = json.dumps(
            entry,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ) + "\n"

        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        self._transaction_count = transaction

    @contextmanager
    def transaction(self) -> Iterator[MemoryUnitOfWork]:
        self._refresh_from_journal()
        before = self._state
        try:
            uow = MemoryUnitOfWork(fork_state(self._state), self)
            yield uow
            if not uow._closed:
                uow.commit()
            changes = changes_for_dirty_records(
                before,
                self._state,
                uow.dirty_records,
            )
            self._append_transaction(changes)
        except Exception:
            self._state = before
            raise

    def journal_transaction_count(self) -> int:
        self._refresh_from_journal()
        return self._transaction_count

    def job_state(self, job_id: str):
        self._refresh_from_journal()
        return super().job_state(job_id)

    def domain_delivery(self, mutation_id: str):
        self._fresh()
        return super().domain_delivery(mutation_id)

    def domain_deliveries(self):
        self._fresh()
        return super().domain_deliveries()

    def sink_delivery(self, delivery_id: str):
        self._refresh_from_journal()
        return super().sink_delivery(delivery_id)

    def sink_deliveries(self, *, job_id=None, sink_name=None):
        self._refresh_from_journal()
        return super().sink_deliveries(job_id=job_id, sink_name=sink_name)

    def sink_checkpoint(self, job_id: str, sink_name: str):
        self._refresh_from_journal()
        return super().sink_checkpoint(job_id, sink_name)

    def sink_checkpoints(self, *, job_id=None):
        self._refresh_from_journal()
        return super().sink_checkpoints(job_id=job_id)

    def business_effect(self, effect_id: str):
        self._refresh_from_journal()
        return super().business_effect(effect_id)

    def business_effects(self):
        self._refresh_from_journal()
        return super().business_effects()

    def job_states(self):
        self._refresh_from_journal()
        return super().job_states()

    def committed_tick(self) -> int:
        self._refresh_from_journal()
        return super().committed_tick()

    def events(self):
        self._refresh_from_journal()
        return super().events()

    def entity(self, entity_type: str, entity_id: str):
        self._refresh_from_journal()
        return super().entity(entity_type, entity_id)

    def command(self, command_id: str):
        self._refresh_from_journal()
        return super().command(command_id)

    def scheduled_work(self):
        self._refresh_from_journal()
        return super().scheduled_work()

    def due_scheduled_work(self, at):
        self._refresh_from_journal()
        return tuple(
            work for work in super().scheduled_work()
            if work.due_at <= at
        )

    def simulation_position(self):
        self._refresh_from_journal()
        return super().simulation_position()

    def scenario_state(self):
        self._refresh_from_journal()
        return super().scenario_state()

    def resource_definitions(self):
        self._refresh_from_journal()
        return super().resource_definitions()

    def resource_demands(self):
        self._refresh_from_journal()
        return super().resource_demands()

    def resource_reservations(self):
        self._refresh_from_journal()
        return super().resource_reservations()

    def resource_release_intents(self):
        self._refresh_from_journal()
        return super().resource_release_intents()

    def store_definitions(self):
        self._refresh_from_journal()
        return super().store_definitions()

    def store_items(self):
        self._refresh_from_journal()
        return super().store_items()

    def store_put_intents(self):
        self._refresh_from_journal()
        return super().store_put_intents()

    def store_get_requests(self):
        self._refresh_from_journal()
        return super().store_get_requests()

    def store_get_results(self):
        self._refresh_from_journal()
        return super().store_get_results()

    def container_definitions(self):
        self._refresh_from_journal()
        return super().container_definitions()

    def container_states(self):
        self._refresh_from_journal()
        return super().container_states()

    def container_operation_intents(self):
        self._refresh_from_journal()
        return super().container_operation_intents()

    def container_operation_results(self):
        self._refresh_from_journal()
        return super().container_operation_results()

    def preemptive_resource_definitions(self):
        self._refresh_from_journal()
        return super().preemptive_resource_definitions()

    def preemptive_resource_demands(self):
        self._refresh_from_journal()
        return super().preemptive_resource_demands()

    def preemptive_resource_reservations(self):
        self._refresh_from_journal()
        return super().preemptive_resource_reservations()

    def preemptive_resource_release_intents(self):
        self._refresh_from_journal()
        return super().preemptive_resource_release_intents()

    def resource_preemption_results(self):
        self._refresh_from_journal()
        return super().resource_preemption_results()
