"""ADR-0009 offline migration of SOSE <= 0.8 SQLite databases (real 0.8.0 fixtures)."""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from sose.backends.simpy import SimPyBackend
from sose.cli import run_cli
from sose.examples.tutorial_job.simulation import ORIGIN, build_runtime
from sose.persistence.owner_migration import OwnerMigrationError, migrate_unowned_sqlite
from sose.persistence.sqlite import SQLitePersistence

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "sqlite"


def _copy(tmp_path, name):
    path = tmp_path / f"{name}.sqlite3"
    shutil.copyfile(FIXTURES / f"{name}.sqlite3", path)
    return path


def test_midflight_080_database_migrates_and_the_job_finishes_under_the_owner(tmp_path):
    source = _copy(tmp_path, "sose-0.8.0-midflight")
    before = source.read_bytes()
    output = tmp_path / "owned.sqlite3"
    report = migrate_unowned_sqlite(source, output, owner_id="tutorial")

    assert source.read_bytes() == before                       # source untouched
    assert Path(report.rollback_copy).read_bytes() == before  # byte copy kept
    assert report.owned_records == 3                           # work + command + event
    with SQLitePersistence(output) as persistence:
        [job] = [e for e in persistence.entities() if e.entity_type == "tutorial_job"]
        assert job.state == "running" and len(persistence.scheduled_work()) == 1
        _, engine = build_runtime(persistence, owner_id="tutorial")
        backend = SimPyBackend(origin=ORIGIN)
        engine.rebuild_backend(backend)
        backend.run_until(ORIGIN + timedelta(hours=2))
        assert persistence.entity("tutorial_job", job.id).state == "completed"
        assert persistence.scheduled_work() == ()


def test_identities_are_preserved_and_only_ownership_is_added(tmp_path):
    source = _copy(tmp_path, "sose-0.8.0-midflight")
    output = tmp_path / "owned.sqlite3"
    migrate_unowned_sqlite(source, output, owner_id="tutorial")

    def payload(path):
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return json.loads(connection.execute("SELECT payload FROM sose_state").fetchone()[0])
        finally:
            connection.close()

    old, new = payload(source)["fields"], payload(output)["fields"]
    for collection in ("commands", "scheduled_work", "entities"):
        assert [k for k, _ in old[collection]["items"]] == [k for k, _ in new[collection]["items"]]
    assert [e["fields"]["event_id"] for e in old["events"]["items"]] == [
        e["fields"]["event_id"] for e in new["events"]["items"]
    ]
    assert all(e["fields"]["owner_id"] == "tutorial" for e in new["events"]["items"])
    assert {"committed_tick", "simulation_position", "scenario_state"}.isdisjoint(new)


def test_seeded_database_migrates_without_inventing_positions(tmp_path):
    output = tmp_path / "owned.sqlite3"
    migrate_unowned_sqlite(_copy(tmp_path, "sose-0.8.0-seeded"), output, owner_id="tutorial")
    with SQLitePersistence(output) as persistence:
        assert persistence.committed_tick(owner_id="tutorial") == -1
        assert persistence.simulation_position(owner_id="tutorial") is None


@pytest.mark.parametrize(
    ("owner", "make_output", "message"),
    [
        ("", lambda tmp, src: tmp / "o.sqlite3", "non-empty owner_id"),
        ("x", lambda tmp, src: src, "never the source"),
        ("x", lambda tmp, src: (tmp / "exists.sqlite3").write_bytes(b"") or tmp / "exists.sqlite3",
         "refusing to overwrite"),
    ],
)
def test_bad_arguments_are_refused_before_anything_is_written(
    tmp_path, owner, make_output, message,
):
    source = _copy(tmp_path, "sose-0.8.0-midflight")
    output = make_output(tmp_path, source)
    with pytest.raises(OwnerMigrationError, match=message):
        migrate_unowned_sqlite(source, output, owner_id=owner)
    assert not (source.parent / (source.name + ".pre-owner-migration")).exists()


def test_owner_qualified_database_is_not_migrated_again(tmp_path):
    output = tmp_path / "owned.sqlite3"
    migrate_unowned_sqlite(_copy(tmp_path, "sose-0.8.0-midflight"), output, owner_id="tutorial")
    with pytest.raises(OwnerMigrationError, match="nothing to migrate"):
        migrate_unowned_sqlite(output, tmp_path / "again.sqlite3", owner_id="tutorial")


def test_database_held_by_a_writer_is_refused(tmp_path):
    source = _copy(tmp_path, "sose-0.8.0-midflight")
    writer = sqlite3.connect(source, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(OwnerMigrationError, match="stop every worker"):
            migrate_unowned_sqlite(source, tmp_path / "o.sqlite3", owner_id="tutorial")
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert not (tmp_path / "o.sqlite3").exists()


def test_cli_migrates_and_writes_the_report(tmp_path, capsys):
    source = _copy(tmp_path, "sose-0.8.0-midflight")
    report = tmp_path / "report.json"
    assert run_cli([
        "migrate-owners", "--sqlite", str(source), "--output", str(tmp_path / "o.sqlite3"),
        "--owner", "tutorial", "--report", str(report),
    ]) == 0
    data = json.loads(report.read_text())
    assert data["owner_id"] == "tutorial" and data["owned_records"] == 3
    assert json.loads(capsys.readouterr().out) == data


def test_refusal_message_points_at_the_migration_command(tmp_path):
    from sose.persistence.sqlite_migrations import LegacyUnownedSnapshotError

    with pytest.raises(LegacyUnownedSnapshotError, match="sose migrate-owners"):
        SQLitePersistence(_copy(tmp_path, "sose-0.8.0-seeded"))


def _rewrite_payload(path, mutate):
    connection = sqlite3.connect(path)
    payload = json.loads(connection.execute("SELECT payload FROM sose_state").fetchone()[0])
    mutate(payload)
    connection.execute("UPDATE sose_state SET payload = ?", (json.dumps(payload),))
    connection.commit()
    connection.close()


def test_record_already_owned_by_someone_else_is_refused(tmp_path):
    source = _copy(tmp_path, "sose-0.8.0-midflight")

    def claim_event(payload):
        payload["fields"]["events"]["items"][0]["fields"]["owner_id"] = "someone-else"

    _rewrite_payload(source, claim_event)
    with pytest.raises(OwnerMigrationError, match="already owned by 'someone-else'"):
        migrate_unowned_sqlite(source, tmp_path / "o.sqlite3", owner_id="tutorial")
    assert not (tmp_path / "o.sqlite3").exists()


def test_database_without_state_is_refused(tmp_path):
    source = _copy(tmp_path, "sose-0.8.0-seeded")
    connection = sqlite3.connect(source)
    connection.execute("DELETE FROM sose_state")
    connection.commit()
    connection.close()
    with pytest.raises(OwnerMigrationError, match="has no SOSE durable state"):
        migrate_unowned_sqlite(source, tmp_path / "o.sqlite3", owner_id="tutorial")


def test_missing_source_is_refused(tmp_path):
    with pytest.raises(OwnerMigrationError, match="source database not found"):
        migrate_unowned_sqlite(tmp_path / "nope.sqlite3", tmp_path / "o.sqlite3", owner_id="x")


def test_broken_referential_integrity_fails_verification_and_leaves_no_output(tmp_path):
    """Scheduled work whose Command is gone must not be handed to a live runtime."""
    source = _copy(tmp_path, "sose-0.8.0-midflight")

    def drop_commands(payload):
        payload["fields"]["commands"]["items"] = []

    _rewrite_payload(source, drop_commands)
    output = tmp_path / "o.sqlite3"
    with pytest.raises(OwnerMigrationError, match="failed runtime diagnostics"):
        migrate_unowned_sqlite(source, output, owner_id="tutorial")
    assert not output.exists()
