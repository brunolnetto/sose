from __future__ import annotations

from pathlib import Path

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
from tests.support.chaos import InjectedProcessCrash, TransactionChaosSQLite


CANONICALS = builtin_catalog().names(kind="canonical")
TICKS = 3


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


def _run_clean(definition, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    persistence = TransactionChaosSQLite(path)
    job = _job(definition, persistence)
    job.initialize()
    for tick in range(1, TICKS + 1):
        job.run_tick(trigger_id=_trigger(definition.name, tick))
    snapshot = operational_snapshot(persistence)
    transaction_count = persistence.transaction_count
    persistence.close()
    return snapshot, transaction_count


def _run_crash_case(definition, path: Path, *, boundary: int, phase: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    persistence = TransactionChaosSQLite(
        path,
        crash_at_transaction=boundary,
        crash_phase=phase,
    )
    job = _job(definition, persistence)
    active_tick = 0
    try:
        job.initialize()
        for tick in range(1, TICKS + 1):
            active_tick = tick
            job.run_tick(trigger_id=_trigger(definition.name, tick))
    except InjectedProcessCrash:
        assert persistence.crashed
        persistence.close()
    else:
        persistence.close()
        raise AssertionError(
            f"chaos boundary was not reached: {definition.name} {phase} #{boundary}"
        )

    reopened = SQLiteIncrementalPersistence(path)
    resumed = _job(definition, reopened)
    if active_tick == 0:
        resumed.initialize()
        next_tick = 1
    else:
        resumed.run_tick(
            trigger_id=_trigger(definition.name, active_tick),
            recover=True,
        )
        next_tick = active_tick + 1

    for tick in range(next_tick, TICKS + 1):
        resumed.run_tick(trigger_id=_trigger(definition.name, tick))

    snapshot = operational_snapshot(reopened)
    reopened.close()
    return snapshot


@pytest.mark.parametrize("name", CANONICALS)
def test_every_engine_transaction_boundary_recovers_to_control_state(tmp_path, name):
    definition = builtin_catalog().get(name)
    expected, transaction_count = _run_clean(
        definition,
        tmp_path / name / "control.sqlite3",
    )
    assert transaction_count > 0

    for phase in ("before_commit", "after_commit"):
        for boundary in range(1, transaction_count + 1):
            actual = _run_crash_case(
                definition,
                tmp_path / name / f"{phase}-{boundary}.sqlite3",
                boundary=boundary,
                phase=phase,
            )
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
