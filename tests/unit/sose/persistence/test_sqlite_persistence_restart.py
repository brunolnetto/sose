from sose.backends.simpy import SimPyBackend
from sose.examples.itsm.simulation import (
    ORIGIN,
    build_runtime,
    claim_next_incident,
    reconcile_escalation,
    seed_reference,
    triage_and_queue,
)
from sose.persistence.sqlite import SQLitePersistence
from sose.testing.restart import restart_reference_runtime


def test_sqlite_state_survives_close_and_reopen(tmp_path):
    path = tmp_path / "sose.sqlite3"
    with SQLitePersistence(path) as first:
        entities = seed_reference(first)
        incident = first.entity("itsm_incident", entities.incident_id)
        assert incident is not None

    with SQLitePersistence(path) as reopened:
        restored = reopened.entity("itsm_incident", entities.incident_id)
        assert restored == incident


def test_reference_restart_crosses_sqlite_process_boundary(tmp_path):
    path = tmp_path / "reference.sqlite3"
    with SQLitePersistence(path) as persistence:
        entities = seed_reference(persistence)
        _, engine = build_runtime(persistence)
        backend_before = SimPyBackend(origin=ORIGIN)
        engine.rebuild_backend(backend_before)

        sla_at = triage_and_queue(
            persistence,
            engine,
            backend_before,
            incident_id=entities.incident_id,
        )
        assert claim_next_incident(
            persistence,
            engine,
            backend_before,
            claim_id="sqlite-restart",
        ) == entities.incident_id
        assert persistence.entity(
            "itsm_incident",
            entities.incident_id,
        ).state == "in_progress"

    with SQLitePersistence(path) as reopened:
        rebuilt = restart_reference_runtime(
            reopened,
            build_runtime,
            backend_before,
            backend_factory=SimPyBackend,
        )
        rebuilt.backend.run_until(sla_at)

        incident = reopened.entity("itsm_incident", entities.incident_id)
        assert incident is not None and incident.state == "escalated"
        assert reconcile_escalation(
            reopened,
            rebuilt.engine,
            rebuilt.backend,
            incident_id=entities.incident_id,
            claim_id="sqlite-restart",
        )
        assert reopened.resource_demands() == ()
        assert reopened.resource_reservations() == ()
