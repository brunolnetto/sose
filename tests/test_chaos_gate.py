from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
from typing import Iterator

import pytest

from sose.backends.simpy import SimPyBackend
from sose.domain.entity import Entity
from sose.domain.outbox import DomainMutationOutbox
from sose.domain.sqlite import SQLiteDomainWarehouse
from sose.domain.warehouse import DomainApplyResult, DomainMutation
from sose.examples.catalog import builtin_catalog
from sose.jobs.runner import SimulationJob
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence
from tests.support.behavioral_conformance import operational_snapshot
from tests.support.chaos import chaos_canonical_names


CANONICALS = chaos_canonical_names()
TICKS = 3
EXHAUSTIVE_BOUNDARY_CHAOS = os.environ.get("SOSE_EXHAUSTIVE_CHAOS_GATE") == "1"
EXTENDED_BOUNDARY_CHAOS = os.environ.get("SOSE_EXTENDED_CHAOS_GATE") == "1"


def _backend(origin):
    return SimPyBackend(origin=origin)


def _job(definition, persistence):
    return SimulationJob(
        job_id=f"{definition.name}-chaos-gate",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )


def _trigger(name: str, tick: int) -> str:
    return f"{name}:chaos:{tick}"


class _CheckpointingSQLite(SQLiteIncrementalPersistence):
    def __init__(self, path: Path, *, checkpoints_dir: Path) -> None:
        self.transaction_count = 0
        self._database_path = path
        self._checkpoints_dir = checkpoints_dir
        self._checkpoints_dir.mkdir(parents=True, exist_ok=True)
        super().__init__(path)
        self._checkpoint(0)

    def _checkpoint(self, index: int) -> None:
        destination = self._checkpoints_dir / f"tx-{index}.sqlite3"
        shutil.copy2(self._database_path, destination)

    def checkpoint_for(self, index: int) -> Path:
        return self._checkpoints_dir / f"tx-{index}.sqlite3"

    @contextmanager
    def transaction(self, *, owner_epoch: int | None = None) -> Iterator:
        self.transaction_count += 1
        transaction_index = self.transaction_count
        with super().transaction(owner_epoch=owner_epoch) as uow:
            yield uow
        self._checkpoint(transaction_index)


def _run_clean(definition, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    checkpoints_dir = path.parent / "checkpoints"
    persistence = _CheckpointingSQLite(path, checkpoints_dir=checkpoints_dir)
    job = _job(definition, persistence)
    job.initialize()
    for tick in range(1, TICKS + 1):
        job.run_tick(trigger_id=_trigger(definition.name, tick))
    snapshot = operational_snapshot(persistence)
    transaction_count = persistence.transaction_count
    checkpoints = {
        index: persistence.checkpoint_for(index)
        for index in range(0, transaction_count + 1)
    }
    persistence.close()
    return snapshot, transaction_count, checkpoints


def _recover_from_checkpoint(definition, path: Path):
    reopened = SQLiteIncrementalPersistence(path)
    resumed = _job(definition, reopened)

    state = resumed.state()
    if state is None or not state.initialized:
        resumed.initialize()

    while True:
        state = resumed.state()
        assert state is not None
        if state.next_tick >= TICKS and state.active_trigger_id is None:
            break
        if state.active_trigger_id is not None:
            resumed.run_tick(
                trigger_id=state.active_trigger_id,
                recover=True,
            )
        else:
            next_tick = state.next_tick + 1
            resumed.run_tick(trigger_id=_trigger(definition.name, next_tick))

    snapshot = operational_snapshot(reopened)
    reopened.close()
    return snapshot


def _boundaries_to_check(transaction_count: int) -> tuple[int, ...]:
    assert transaction_count > 0
    if EXHAUSTIVE_BOUNDARY_CHAOS:
        return tuple(range(1, transaction_count + 1))
    boundaries = {1, transaction_count}
    if EXTENDED_BOUNDARY_CHAOS:
        boundaries.add(max(1, transaction_count // 2))
    return tuple(sorted(boundaries))


@pytest.mark.slow
@pytest.mark.parametrize("name", CANONICALS)
def test_every_engine_transaction_boundary_recovers_to_control_state(tmp_path, name):
    """Validate boundary crash recovery.

    Runs first/last boundaries by default for runtime control, can include the
    midpoint with SOSE_EXTENDED_CHAOS_GATE=1, and can run exhaustively with
    SOSE_EXHAUSTIVE_CHAOS_GATE=1.
    """
    definition = builtin_catalog().get(name)
    expected, transaction_count, checkpoints = _run_clean(
        definition,
        tmp_path / name / "control.sqlite3",
    )
    assert transaction_count > 0

    boundaries = _boundaries_to_check(transaction_count)
    checkpoint_indices = {
        boundary if phase == "after_commit" else boundary - 1
        for phase in ("before_commit", "after_commit")
        for boundary in boundaries
    }
    actual_by_checkpoint: dict[int, object] = {}
    for checkpoint_index in sorted(checkpoint_indices):
        checkpoint_source = checkpoints[checkpoint_index]
        case_path = tmp_path / name / "recovery" / f"checkpoint-{checkpoint_index}.sqlite3"
        case_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(checkpoint_source, case_path)
        actual_by_checkpoint[checkpoint_index] = _recover_from_checkpoint(
            definition,
            case_path,
        )

    for phase in ("before_commit", "after_commit"):
        for boundary in boundaries:
            checkpoint_index = boundary if phase == "after_commit" else boundary - 1
            actual = actual_by_checkpoint[checkpoint_index]
            assert actual == expected, (
                f"{name} diverged after {phase} crash at transaction {boundary}"
            )


def _mutation() -> DomainMutation:
    return DomainMutation(
        "chaos:work-order:wo-1:v0",
        Entity(
            id="wo-1",
            entity_type="work_order",
            state="released",
        ),
    )


class _FailOnceWarehouse:
    def __init__(self, delegate: SQLiteDomainWarehouse) -> None:
        self.delegate = delegate
        self.failed = False

    def entity(self, entity_type: str, entity_id: str):
        return self.delegate.entity(entity_type, entity_id)

    def entities(self, entity_type: str | None = None):
        return self.delegate.entities(entity_type)

    def apply(self, mutation: DomainMutation):
        if not self.failed:
            self.failed = True
            raise RuntimeError("injected domain warehouse outage")
        return self.delegate.apply(mutation)


def test_dual_store_recovers_when_domain_apply_fails_before_commit(tmp_path):
    engine_path = tmp_path / "domain-failure-engine.sqlite3"
    domain_path = tmp_path / "domain-failure-domain.sqlite3"
    engine = SQLiteIncrementalPersistence(engine_path)
    warehouse = SQLiteDomainWarehouse(domain_path)
    outbox = DomainMutationOutbox(engine, _FailOnceWarehouse(warehouse))
    delivery = outbox.prepare(_mutation())

    with pytest.raises(RuntimeError, match="injected domain warehouse outage"):
        outbox.deliver(delivery)
    pending = engine.domain_delivery(delivery.mutation_id)
    assert pending is not None
    assert pending.attempts == 1
    assert warehouse.entity("work_order", "wo-1") is None
    engine.close()
    warehouse.close()

    engine = SQLiteIncrementalPersistence(engine_path)
    warehouse = SQLiteDomainWarehouse(domain_path)
    restarted = DomainMutationOutbox(engine, warehouse)
    assert restarted.flush() == 1
    assert engine.domain_deliveries() == ()
    assert warehouse.entity("work_order", "wo-1").state == "released"
    engine.close()
    warehouse.close()


def test_dual_store_replays_when_process_dies_after_apply_before_ack(tmp_path):
    engine_path = tmp_path / "after-apply-engine.sqlite3"
    domain_path = tmp_path / "after-apply-domain.sqlite3"
    engine = SQLiteIncrementalPersistence(engine_path)
    warehouse = SQLiteDomainWarehouse(domain_path)
    outbox = DomainMutationOutbox(engine, warehouse)
    delivery = outbox.prepare(_mutation())

    assert warehouse.apply(delivery.mutation) is DomainApplyResult.APPLIED
    assert engine.domain_delivery(delivery.mutation_id) is not None
    engine.close()
    warehouse.close()

    engine = SQLiteIncrementalPersistence(engine_path)
    warehouse = SQLiteDomainWarehouse(domain_path)
    restarted = DomainMutationOutbox(engine, warehouse)
    pending = restarted.pending()
    assert len(pending) == 1
    assert restarted.deliver(pending[0]) is DomainApplyResult.REPLAYED
    assert engine.domain_deliveries() == ()
    assert warehouse.entity("work_order", "wo-1").state == "released"
    engine.close()
    warehouse.close()
