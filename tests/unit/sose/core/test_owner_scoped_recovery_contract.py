"""#504 — contract-level RED: same-type owners must not share a recovery clock.

The test deliberately exercises the public owner-scoped recovery contract that
does not yet exist. A GREEN requires authoritative owner-qualified position and
ScheduledWork reads, not a clock-mismatch bypass or global serialization.
"""
from datetime import datetime, timedelta, timezone

from sose.core.durable import RuntimeRebuilder
from sose.core.runtime import SimulationPosition
from sose.persistence.memory import MemoryPersistence


ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


class RecordingBackend:
    def __init__(self, now):
        self.now = now
        self.calls = []

    def schedule_at(self, at, callback, *, priority=100, key=None):
        self.calls.append((at, priority, key, callback))


def test_independent_same_type_owner_cannot_inherit_foreign_persisted_clock():
    """RED: A may rebuild at its own boundary after B advances its clock.

    Current runtime reads one namespace-wide SimulationPosition, causing
    RuntimeError or incorrectly treating B's time as A's recovery boundary.
    """
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.set_simulation_position(SimulationPosition(
            logical_time=ORIGIN + timedelta(hours=2),
            execution_sequence=2,
            committed_sequence=2,
            logical_tick=2,
        ))
    backend_a = RecordingBackend(ORIGIN + timedelta(hours=1))
    # Desired future contract: owner_id determines the authoritative position.
    # Owner A has not advanced merely because B committed at hour 2.
    rebuilt = RuntimeRebuilder(
        store, owner_id="organization-a",
    ).rebuild(backend_a, on_due=lambda _: None)
    assert rebuilt == 0
    assert backend_a.now == ORIGIN + timedelta(hours=1)
