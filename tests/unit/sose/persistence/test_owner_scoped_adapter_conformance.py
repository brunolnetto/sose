"""Cross-adapter owner-qualified temporal authority survives full reconstruction."""
from datetime import datetime, timedelta, timezone

import pytest

from sose.core.runtime import SimulationPosition
from sose.persistence.duckdb import DuckDBPersistence
from sose.persistence.jsonl_journal import JSONLJournalPersistence
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence
from sose.scenarios.model import ScenarioRuntimeState


ORIGIN = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def _position(hours: int, sequence: int) -> SimulationPosition:
    return SimulationPosition(
        logical_time=ORIGIN + timedelta(hours=hours),
        execution_sequence=sequence,
        committed_sequence=sequence,
        logical_tick=sequence,
    )


def _close(store) -> None:
    close = getattr(store, "close", None)
    if callable(close):
        close()


@pytest.mark.parametrize("adapter,suffix", [
    (SQLitePersistence, ".sqlite3"),
    (SQLiteIncrementalPersistence, ".sqlite3"),
    (DuckDBPersistence, ".duckdb"),
    (JSONLJournalPersistence, ".jsonl"),
])
def test_owner_positions_ticks_and_scenarios_are_isolated_across_reopen(
    tmp_path, adapter, suffix,
):
    path = tmp_path / ("temporal" + suffix)
    first = adapter(path)
    scenario_a = ScenarioRuntimeState()
    position_a, position_b = _position(1, 1), _position(3, 3)
    try:
        with first.transaction() as uow:
            uow.set_simulation_position(position_a, owner_id="organization-a")
            uow.set_simulation_position(position_b, owner_id="organization-b")
            uow.set_committed_tick(1, owner_id="organization-a")
            uow.set_committed_tick(3, owner_id="organization-b")
            uow.set_scenario_state(scenario_a, owner_id="organization-a")
    finally:
        _close(first)

    reopened = adapter(path)
    try:
        assert reopened.simulation_position(owner_id="organization-a") == position_a
        assert reopened.simulation_position(owner_id="organization-b") == position_b
        assert reopened.committed_tick(owner_id="organization-a") == 1
        assert reopened.committed_tick(owner_id="organization-b") == 3
        assert reopened.scenario_state(owner_id="organization-a") == scenario_a
        assert reopened.scenario_state(owner_id="organization-b") is None

        with reopened.transaction() as uow:
            uow.set_simulation_position(_position(4, 4), owner_id="organization-b")
            uow.set_committed_tick(4, owner_id="organization-b")

        assert reopened.simulation_position(owner_id="organization-a") == position_a
        assert reopened.committed_tick(owner_id="organization-a") == 1
        assert reopened.simulation_position(owner_id="organization-b") == _position(4, 4)
        assert reopened.committed_tick(owner_id="organization-b") == 4

        # No scalar/global recovery boundary or committed-tick compatibility.
        with pytest.raises(TypeError):
            reopened.simulation_position()
        with pytest.raises(TypeError):
            reopened.committed_tick()
        with pytest.raises(TypeError):
            reopened.scenario_state()
    finally:
        _close(reopened)
