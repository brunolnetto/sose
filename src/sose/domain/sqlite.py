from __future__ import annotations

from pathlib import Path
import sqlite3

from sose.persistence.codec import dumps, loads

from .entity import Entity
from .warehouse import DomainApplyResult, DomainMutation


class SQLiteDomainWarehouse:
    """Durable SQLite current-state store for simulated business entities."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, isolation_level=None, timeout=30.0)
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS domain_entity (
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(entity_type, entity_id)
            )"""
        )
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS domain_mutation (
                mutation_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )"""
        )

    def close(self) -> None:
        self._connection.close()

    def entity(self, entity_type: str, entity_id: str) -> Entity | None:
        row = self._connection.execute(
            "SELECT payload FROM domain_entity WHERE entity_type=? AND entity_id=?",
            (entity_type, entity_id),
        ).fetchone()
        return None if row is None else loads(str(row[0]))

    def entities(self, entity_type: str | None = None) -> tuple[Entity, ...]:
        if entity_type is None:
            rows = self._connection.execute(
                "SELECT payload FROM domain_entity ORDER BY entity_type, entity_id"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT payload FROM domain_entity WHERE entity_type=? ORDER BY entity_id",
                (entity_type,),
            ).fetchall()
        return tuple(loads(str(row[0])) for row in rows)

    def apply(self, mutation: DomainMutation) -> DomainApplyResult:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            replay = self._connection.execute(
                "SELECT payload FROM domain_mutation WHERE mutation_id=?",
                (mutation.mutation_id,),
            ).fetchone()
            if replay is not None:
                if loads(str(replay[0])) != mutation:
                    raise ValueError(
                        f"domain mutation identity conflict: {mutation.mutation_id}"
                    )
                self._connection.commit()
                return DomainApplyResult.REPLAYED

            current = self.entity(mutation.entity.entity_type, mutation.entity.id)
            if current is not None:
                if mutation.entity.version < current.version:
                    result = DomainApplyResult.SUPERSEDED
                elif mutation.entity.version == current.version:
                    if mutation.entity != current:
                        raise ValueError(
                            "conflicting domain entity version: "
                            f"{mutation.entity.entity_type}/{mutation.entity.id} "
                            f"v{mutation.entity.version}"
                        )
                    result = DomainApplyResult.REPLAYED
                else:
                    result = DomainApplyResult.APPLIED
            else:
                result = DomainApplyResult.APPLIED

            if result is DomainApplyResult.APPLIED:
                self._connection.execute(
                    """INSERT INTO domain_entity(entity_type, entity_id, version, payload)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                         version=excluded.version, payload=excluded.payload""",
                    (
                        mutation.entity.entity_type,
                        mutation.entity.id,
                        mutation.entity.version,
                        dumps(mutation.entity),
                    ),
                )
            self._connection.execute(
                "INSERT INTO domain_mutation(mutation_id, payload) VALUES (?, ?)",
                (mutation.mutation_id, dumps(mutation)),
            )
            self._connection.commit()
            return result
        except Exception:
            self._connection.rollback()
            raise
