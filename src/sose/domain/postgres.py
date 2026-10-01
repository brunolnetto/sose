from __future__ import annotations

import re

import psycopg
from psycopg import sql

from sose.persistence.codec import dumps, loads

from .entity import Entity
from .warehouse import DomainApplyResult, DomainMutation

_NAMESPACE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")


class PostgresDomainWarehouse:
    """PostgreSQL current-state warehouse with per-entity serialization."""

    def __init__(self, dsn: str, *, namespace: str = "sose_domain") -> None:
        if not dsn:
            raise ValueError("PostgresDomainWarehouse dsn cannot be empty")
        if not _NAMESPACE_RE.fullmatch(namespace):
            raise ValueError("PostgresDomainWarehouse namespace must be SQL-safe")
        self.namespace = namespace
        self._entity_table = sql.Identifier(f"{namespace}_entity")
        self._mutation_table = sql.Identifier(f"{namespace}_mutation")
        self._connection = psycopg.connect(dsn, autocommit=True)
        with self._connection.transaction():
            self._connection.execute(sql.SQL(
                "CREATE TABLE IF NOT EXISTS {} (entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, version BIGINT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(entity_type, entity_id))"
            ).format(self._entity_table))
            self._connection.execute(sql.SQL(
                "CREATE TABLE IF NOT EXISTS {} (mutation_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            ).format(self._mutation_table))

    def close(self) -> None:
        self._connection.close()

    def entity(self, entity_type: str, entity_id: str) -> Entity | None:
        row = self._connection.execute(
            sql.SQL("SELECT payload FROM {} WHERE entity_type=%s AND entity_id=%s").format(self._entity_table),
            (entity_type, entity_id),
        ).fetchone()
        return None if row is None else loads(str(row[0]))

    def entities(self, entity_type: str | None = None) -> tuple[Entity, ...]:
        if entity_type is None:
            rows = self._connection.execute(sql.SQL(
                "SELECT payload FROM {} ORDER BY entity_type, entity_id"
            ).format(self._entity_table)).fetchall()
        else:
            rows = self._connection.execute(sql.SQL(
                "SELECT payload FROM {} WHERE entity_type=%s ORDER BY entity_id"
            ).format(self._entity_table), (entity_type,)).fetchall()
        return tuple(loads(str(row[0])) for row in rows)

    def apply(self, mutation: DomainMutation) -> DomainApplyResult:
        with self._connection.transaction():
            mutation_lock = f"{self.namespace}:mutation:{mutation.mutation_id}"
            self._connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (mutation_lock,)
            )
            replay = self._connection.execute(
                sql.SQL("SELECT payload FROM {} WHERE mutation_id=%s FOR UPDATE").format(self._mutation_table),
                (mutation.mutation_id,),
            ).fetchone()
            if replay is not None:
                if loads(str(replay[0])) != mutation:
                    raise ValueError(f"domain mutation identity conflict: {mutation.mutation_id}")
                return DomainApplyResult.REPLAYED

            # Serialize writers for this logical entity even when the row does
            # not exist yet; row locking alone cannot protect the insert race.
            lock_key = f"{self.namespace}:{mutation.entity.entity_type}:{mutation.entity.id}"
            self._connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))
            row = self._connection.execute(
                sql.SQL("SELECT payload FROM {} WHERE entity_type=%s AND entity_id=%s FOR UPDATE").format(self._entity_table),
                (mutation.entity.entity_type, mutation.entity.id),
            ).fetchone()
            current = None if row is None else loads(str(row[0]))
            if current is not None and mutation.entity.version < current.version:
                result = DomainApplyResult.SUPERSEDED
            elif current is not None and mutation.entity.version == current.version:
                if mutation.entity != current:
                    raise ValueError(
                        "conflicting domain entity version: "
                        f"{mutation.entity.entity_type}/{mutation.entity.id} v{mutation.entity.version}"
                    )
                result = DomainApplyResult.REPLAYED
            else:
                result = DomainApplyResult.APPLIED

            if result is DomainApplyResult.APPLIED:
                self._connection.execute(sql.SQL(
                    """INSERT INTO {}(entity_type, entity_id, version, payload)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                         version=EXCLUDED.version, payload=EXCLUDED.payload"""
                ).format(self._entity_table), (
                    mutation.entity.entity_type, mutation.entity.id,
                    mutation.entity.version, dumps(mutation.entity),
                ))
            self._connection.execute(
                sql.SQL("INSERT INTO {}(mutation_id,payload) VALUES (%s,%s)").format(self._mutation_table),
                (mutation.mutation_id, dumps(mutation)),
            )
            return result
