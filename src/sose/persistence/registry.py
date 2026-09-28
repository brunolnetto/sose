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
class PersistenceAdapter:
    name: str
    factory: PersistenceFactory
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

    def create(
        self,
        name: str,
        options: dict[str, object] | None = None,
        *,
        base_dir: Path,
    ) -> Persistence:
        try:
            adapter = self._adapters[name]
        except KeyError as exc:
            raise KeyError(f"unknown persistence adapter: {name}") from exc
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
        )
    )
    registry.register(
        PersistenceAdapter(
            "sqlite",
            lambda options, base_dir: SQLitePersistence(
                _resolve_path(options, base_dir, default="sose.sqlite3")
            ),
        )
    )
    registry.register(
        PersistenceAdapter(
            "sqlite_incremental",
            lambda options, base_dir: SQLiteIncrementalPersistence(
                _resolve_path(options, base_dir, default="sose.sqlite3")
            ),
        )
    )
    registry.register(
        PersistenceAdapter(
            "jsonl",
            lambda options, base_dir: JSONLJournalPersistence(
                _resolve_path(options, base_dir, default="sose.jsonl")
            ),
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
            optional_extra="duckdb",
        )
    )
    return registry
