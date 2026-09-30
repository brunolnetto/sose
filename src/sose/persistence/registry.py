from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sose.persistence.base import Persistence
from sose.persistence.jsonl_journal import JSONLJournalPersistence
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


PersistenceFactory = Callable[[dict[str, object], Path], Persistence]


@dataclass(frozen=True, slots=True)
class PersistenceCapabilities:
    process_durable: bool
    transactional_commits: bool
    incremental_updates: bool
    concurrent_writers: bool
    remote: bool
    analytical_reads: bool
    append_only: bool
    schema_migrations: bool
    authoritative_read_after_commit: bool = False
    conditional_writes: bool = False
    durable_job_leases: bool = False
    fencing: bool = False
    restart_reconstructible: bool = False

    def names(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in (
                "process_durable",
                "transactional_commits",
                "incremental_updates",
                "concurrent_writers",
                "remote",
                "analytical_reads",
                "append_only",
                "schema_migrations",
                "authoritative_read_after_commit",
                "conditional_writes",
                "durable_job_leases",
                "fencing",
                "restart_reconstructible",
            )
            if getattr(self, name)
        )


@dataclass(frozen=True, slots=True)
class PersistenceAdapter:
    name: str
    factory: PersistenceFactory
    capabilities: PersistenceCapabilities
    optional_extra: str | None = None


class PersistenceRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, PersistenceAdapter] = {}

    def register(self, adapter: PersistenceAdapter) -> None:
        if adapter.name in self._adapters:
            raise ValueError(f"persistence adapter already registered: {adapter.name}")
        self._adapters[adapter.name] = adapter

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def adapter(self, name: str) -> PersistenceAdapter:
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise KeyError(f"unknown persistence adapter: {name}") from exc

    def capabilities(self, name: str) -> PersistenceCapabilities:
        return self.adapter(name).capabilities

    def describe(self) -> tuple[PersistenceAdapter, ...]:
        return tuple(self._adapters[name] for name in self.names())

    def require(self, name: str, *capabilities: str) -> PersistenceAdapter:
        adapter = self.adapter(name)
        missing = [
            capability
            for capability in capabilities
            if not hasattr(adapter.capabilities, capability)
            or not getattr(adapter.capabilities, capability)
        ]
        if missing:
            raise ValueError(
                f"persistence adapter {name!r} lacks required capabilities: "
                + ", ".join(sorted(missing))
            )
        return adapter

    def create(
        self,
        name: str,
        options: dict[str, object] | None = None,
        *,
        base_dir: Path,
    ) -> Persistence:
        adapter = self.adapter(name)
        try:
            return adapter.factory(options or {}, base_dir)
        except ModuleNotFoundError as exc:
            if adapter.optional_extra is None:
                raise
            raise RuntimeError(
                f"persistence adapter {name!r} requires optional extra "
                f"{adapter.optional_extra!r}"
            ) from exc


def _resolve_path(options: dict[str, object], base_dir: Path, *, default: str) -> Path:
    raw = options.get("path", default)
    if not isinstance(raw, str) or not raw:
        raise ValueError("persistence path must be a non-empty string")
    path = Path(raw)
    return path if path.is_absolute() else (base_dir / path)


def builtin_persistence_registry() -> PersistenceRegistry:
    registry = PersistenceRegistry()
    registry.register(
        PersistenceAdapter(
            "memory",
            lambda options, base_dir: MemoryPersistence(),
            capabilities=PersistenceCapabilities(
                process_durable=False,
                transactional_commits=True,
                incremental_updates=True,
                concurrent_writers=False,
                remote=False,
                analytical_reads=False,
                append_only=False,
                schema_migrations=False,
            ),
        )
    )
    registry.register(
        PersistenceAdapter(
            "sqlite",
            lambda options, base_dir: SQLitePersistence(
                _resolve_path(options, base_dir, default="sose.sqlite3")
            ),
            capabilities=PersistenceCapabilities(
                process_durable=True,
                transactional_commits=True,
                incremental_updates=False,
                concurrent_writers=False,
                remote=False,
                analytical_reads=False,
                append_only=False,
                schema_migrations=True,
            ),
        )
    )
    registry.register(
        PersistenceAdapter(
            "sqlite_incremental",
            lambda options, base_dir: SQLiteIncrementalPersistence(
                _resolve_path(options, base_dir, default="sose.sqlite3")
            ),
            capabilities=PersistenceCapabilities(
                process_durable=True,
                transactional_commits=True,
                incremental_updates=True,
                concurrent_writers=False,
                remote=False,
                analytical_reads=False,
                append_only=False,
                schema_migrations=True,
            ),
        )
    )
    registry.register(
        PersistenceAdapter(
            "jsonl",
            lambda options, base_dir: JSONLJournalPersistence(
                _resolve_path(options, base_dir, default="sose.jsonl")
            ),
            capabilities=PersistenceCapabilities(
                process_durable=True,
                transactional_commits=True,
                incremental_updates=True,
                concurrent_writers=False,
                remote=False,
                analytical_reads=False,
                append_only=True,
                schema_migrations=False,
            ),
        )
    )

    def postgres_factory(options: dict[str, object], base_dir: Path) -> Persistence:
        import os

        from sose.persistence.postgres import PostgresPersistence

        raw_dsn = options.get("dsn")
        if raw_dsn is not None and (not isinstance(raw_dsn, str) or not raw_dsn):
            raise ValueError("PostgreSQL dsn must be a non-empty string")

        dsn_env = options.get("dsn_env", "SOSE_DATABASE_URL")
        if not isinstance(dsn_env, str) or not dsn_env:
            raise ValueError("PostgreSQL dsn_env must be a non-empty string")

        dsn = raw_dsn or os.environ.get(dsn_env)
        if not dsn:
            raise ValueError(
                "PostgreSQL connection string is missing; set "
                f"{dsn_env!r} or persistence.options.dsn"
            )

        namespace = options.get("namespace", "sose")
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("PostgreSQL namespace must be a non-empty string")

        return PostgresPersistence(dsn, namespace=namespace)

    registry.register(
        PersistenceAdapter(
            "postgres",
            postgres_factory,
            capabilities=PersistenceCapabilities(
                process_durable=True,
                transactional_commits=True,
                incremental_updates=True,
                concurrent_writers=True,
                remote=True,
                analytical_reads=False,
                append_only=False,
                schema_migrations=False,
            ),
            optional_extra="postgres",
        )
    )

    def duckdb_factory(options: dict[str, object], base_dir: Path) -> Persistence:
        from sose.persistence.duckdb import DuckDBPersistence

        return DuckDBPersistence(
            _resolve_path(options, base_dir, default="sose.duckdb")
        )

    registry.register(
        PersistenceAdapter(
            "duckdb",
            duckdb_factory,
            capabilities=PersistenceCapabilities(
                process_durable=True,
                transactional_commits=True,
                incremental_updates=True,
                concurrent_writers=False,
                remote=False,
                analytical_reads=True,
                append_only=False,
                schema_migrations=False,
            ),
            optional_extra="duckdb",
        )
    )
    def clickhouse_factory(options: dict[str, object], base_dir: Path) -> Persistence:
        from sose.persistence.clickhouse import ClickHousePersistence

        host = options.get("host", "localhost")
        database = options.get("database", "default")
        if not isinstance(host, str) or not host:
            raise ValueError("ClickHouse host must be a non-empty string")
        if not isinstance(database, str) or not database:
            raise ValueError("ClickHouse database must be a non-empty string")
        return ClickHousePersistence(host=host, database=database)

    registry.register(
        PersistenceAdapter(
            "clickhouse",
            clickhouse_factory,
            capabilities=PersistenceCapabilities(
                process_durable=False,
                transactional_commits=False,
                incremental_updates=False,
                concurrent_writers=True,
                remote=True,
                analytical_reads=True,
                append_only=False,
                schema_migrations=False,
                authoritative_read_after_commit=False,
                conditional_writes=False,
                durable_job_leases=False,
                fencing=False,
                restart_reconstructible=False,
            ),
            optional_extra="clickhouse",
        )
    )
    return registry
