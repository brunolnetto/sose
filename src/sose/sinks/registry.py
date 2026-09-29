from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .base import AnalyticalSink
from .jsonl import JSONLAnalyticalSink


SinkFactory = Callable[[dict[str, object], Path], AnalyticalSink]


@dataclass(frozen=True, slots=True)
class SinkAdapter:
    name: str
    factory: SinkFactory
    optional_extra: str | None = None


class SinkRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, SinkAdapter] = {}

    def register(self, adapter: SinkAdapter) -> None:
        if adapter.name in self._adapters:
            raise ValueError(f"sink adapter already registered: {adapter.name}")
        self._adapters[adapter.name] = adapter

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def adapter(self, name: str) -> SinkAdapter:
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise KeyError(f"unknown sink adapter: {name}") from exc

    def create(
        self,
        name: str,
        options: dict[str, object] | None = None,
        *,
        base_dir: Path,
    ) -> AnalyticalSink:
        adapter = self.adapter(name)
        try:
            return adapter.factory(options or {}, base_dir)
        except ModuleNotFoundError as exc:
            if adapter.optional_extra is None:
                raise
            raise RuntimeError(
                f"sink adapter {name!r} requires optional extra "
                f"{adapter.optional_extra!r}"
            ) from exc


def _resolve_path(options: dict[str, object], base_dir: Path) -> Path:
    raw = options.get("path", "analytics.jsonl")
    if not isinstance(raw, str) or not raw:
        raise ValueError("sink path must be a non-empty string")
    path = Path(raw)
    return path if path.is_absolute() else base_dir / path


def builtin_sink_registry() -> SinkRegistry:
    registry = SinkRegistry()
    registry.register(
        SinkAdapter(
            name="jsonl",
            factory=lambda options, base_dir: JSONLAnalyticalSink(
                _resolve_path(options, base_dir)
            ),
        )
    )
    return registry
