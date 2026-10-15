import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.record_to_report.simulation import (
    ORIGIN,
    build_runtime,
    ensure_adjustment,
    reconcile_close,
    reconcile_item,
    reopen_period,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


REFERENCE_OWNER_ID = "record-to-report-reference-organization"

def test_illegal_accounting_prerequisites_are_rejected():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, owner_id=REFERENCE_OWNER_ID)
    _, engine = build_runtime(persistence, owner_id=REFERENCE_OWNER_ID)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    with pytest.raises(
        RuntimeError,
        match="requires durable posted journal evidence",
    ):
        reconcile_item(
            persistence,
            engine,
            backend,
            entities=entities,
        )

    with pytest.raises(
        RuntimeError,
        match="requires ReconciliationItem",
    ):
        ensure_adjustment(
            persistence,
            engine,
            entities=entities,
        )

    with pytest.raises(RuntimeError, match="only a closed period"):
        reopen_period(
            persistence,
            engine,
            entities=entities,
        )

    assert reconcile_close(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
