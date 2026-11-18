"""Offline migration of an unowned SOSE <= 0.8 SQLite snapshot to an owner-qualified one.

ADR-0009 makes the owner-qualified runtime the only execution model and requires data
that must keep operating to be migrated offline, with an operator-supplied owner. In
SOSE 0.8 a database held exactly one runtime (one scalar clock, tick and scenario
state), so the mapping is one owner for the whole database.

The migration never edits the source: it reads it under an exclusive lock (refusing
while any writer is active), writes a NEW database, keeps a byte copy of the source
next to it, and verifies the result before reporting success:

* every identity (entity, command, work, event) is preserved; only ``owner_id`` is added;
* a semantic digest of the content the migration must not change is equal on both sides;
* the target opens with the current runtime and its diagnostics report no issue.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .codec import loads
from .memory import _State
from .sqlite_migrations import LEGACY_UNOWNED_SNAPSHOT_FIELDS

_TAG = "__sose_type__"
_OWNED_RECORDS = {
    "sose.core.runtime:ScheduledWork",
    "sose.core.events:Command",
    "sose.core.events:DomainEvent",
}
# Scalar runtime fields of the unowned snapshot -> owner-keyed field of the current one.
_SCALAR_TO_OWNED = {
    "committed_tick": "committed_ticks",
    "simulation_position": "simulation_positions",
    "scenario_state": "scenario_states",
}


class OwnerMigrationError(RuntimeError):
    """The source cannot be migrated as requested; nothing was written."""


@dataclass(frozen=True, slots=True)
class OwnerMigrationReport:
    source: str
    output: str
    rollback_copy: str
    owner_id: str
    source_sha256: str
    output_sha256: str
    semantic_digest: str
    owned_records: int

    def to_json(self) -> str:
        return json.dumps({name: getattr(self, name) for name in self.__slots__}, indent=2)


def migrate_unowned_sqlite(
    source: str | Path, output: str | Path, *, owner_id: str,
) -> OwnerMigrationReport:
    """Migrate ``source`` (SOSE <= 0.8 SQLitePersistence) into a new owner-qualified ``output``."""
    source, output = Path(source), Path(output)
    _check_arguments(source, output, owner_id)
    payload = _read_source_exclusively(source)
    legacy = json.loads(payload)
    fields = legacy.get("fields") or {}
    missing = LEGACY_UNOWNED_SNAPSHOT_FIELDS - set(fields)
    if missing:
        raise OwnerMigrationError(
            f"{source} is not an unowned SOSE <= 0.8 snapshot (missing {sorted(missing)}); "
            "nothing to migrate"
        )
    migrated, owned = _owner_qualified(legacy, owner_id)
    state = loads(json.dumps(migrated))
    if not isinstance(state, _State):
        raise OwnerMigrationError("migrated payload does not decode to SOSE durable state")
    digest = _semantic_digest(legacy)
    if _semantic_digest(migrated) != digest:
        raise OwnerMigrationError("migration changed content other than ownership; aborted")
    rollback = source.with_name(source.name + ".pre-owner-migration")
    shutil.copyfile(source, rollback)
    _write_target(output, state)
    _verify_target(output, owner_id)
    return OwnerMigrationReport(
        source=str(source), output=str(output), rollback_copy=str(rollback),
        owner_id=owner_id, source_sha256=_sha256(source), output_sha256=_sha256(output),
        semantic_digest=digest, owned_records=owned,
    )


def _check_arguments(source: Path, output: Path, owner_id: str) -> None:
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise OwnerMigrationError("an explicit, non-empty owner_id is required")
    if not source.is_file():
        raise OwnerMigrationError(f"source database not found: {source}")
    if output.resolve() == source.resolve():
        raise OwnerMigrationError("output must be a new database, never the source")
    if output.exists():
        raise OwnerMigrationError(f"refusing to overwrite existing output: {output}")


def _read_source_exclusively(source: Path) -> str:
    """Read the snapshot under an exclusive lock; fail if any writer holds the database."""
    connection = sqlite3.connect(source, isolation_level=None, timeout=0.0)
    try:
        try:
            connection.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            raise OwnerMigrationError(
                f"{source} is in use; stop every worker before migrating ({exc})"
            ) from exc
        row = connection.execute(
            "SELECT payload FROM sose_state WHERE singleton = 1"
        ).fetchone()
        connection.execute("ROLLBACK")
    finally:
        connection.close()
    if row is None:
        raise OwnerMigrationError(f"{source} has no SOSE durable state")
    return str(row[0])


def _owner_qualified(legacy: dict, owner_id: str) -> tuple[dict, int]:
    """Return the owner-qualified payload and how many records gained the owner."""
    fields = dict(legacy["fields"])
    for scalar, owned in _SCALAR_TO_OWNED.items():
        value = fields.pop(scalar)
        if _is_empty_scalar(scalar, value):
            fields[owned] = {_TAG: "dict", "items": []}
        else:
            fields[owned] = {_TAG: "dict", "items": [[owner_id, value]]}
    counter = [0]
    owned_fields = {key: _add_owner(value, owner_id, counter) for key, value in fields.items()}
    migrated = {**legacy, "fields": owned_fields}
    return migrated, counter[0]


def _is_empty_scalar(scalar: str, value: object) -> bool:
    # -1 is the "nothing committed" default; None means no position was recorded.
    return value is None or (scalar == "committed_tick" and value == -1)


def _add_owner(value: object, owner_id: str, counter: list[int]) -> object:
    if isinstance(value, list):
        return [_add_owner(item, owner_id, counter) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get(_TAG) == "dataclass" and value.get("class") in _OWNED_RECORDS:
        record_fields = value["fields"]
        if record_fields.get("owner_id") not in (None, owner_id):
            raise OwnerMigrationError(
                f"{value['class']} already owned by {record_fields['owner_id']!r}"
            )
        counter[0] += 1
        return {**value, "fields": {**record_fields, "owner_id": owner_id}}
    return {key: _add_owner(item, owner_id, counter) for key, item in value.items()}


def _semantic_digest(payload: dict) -> str:
    """Digest of everything except ownership: the scalars/owned maps and owner_id fields."""
    fields = {
        key: value for key, value in payload["fields"].items()
        if key not in _SCALAR_TO_OWNED and key not in _SCALAR_TO_OWNED.values()
    }
    stripped = _strip_owner(fields)
    canonical = json.dumps(stripped, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _strip_owner(value: object) -> object:
    if isinstance(value, list):
        return [_strip_owner(item) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get(_TAG) == "dataclass" and value.get("class") in _OWNED_RECORDS:
        fields = {k: v for k, v in value["fields"].items() if k != "owner_id"}
        return {**value, "fields": _strip_owner(fields)}
    return {key: _strip_owner(item) for key, item in value.items()}


def _write_target(output: Path, state: _State) -> None:
    from .sqlite import SQLitePersistence

    persistence = SQLitePersistence(output)
    try:
        persistence._connection.execute("BEGIN IMMEDIATE")
        persistence._state = state
        persistence._write_state()
        persistence._connection.commit()
    finally:
        persistence.close()


def _verify_target(output: Path, owner_id: str) -> None:
    from sose.core.diagnostics import collect_runtime_diagnostics

    from .sqlite import SQLitePersistence

    with SQLitePersistence(output) as persistence:
        diagnostics = collect_runtime_diagnostics(persistence, owner_id=owner_id)
    if not diagnostics.healthy:
        codes = ", ".join(sorted({issue.code for issue in diagnostics.issues}))
        output.unlink()
        raise OwnerMigrationError(f"migrated database failed runtime diagnostics: {codes}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
