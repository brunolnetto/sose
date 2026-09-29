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


def _option_or_env(
    options: dict[str, object],
    key: str,
    env_key: str,
    *,
    required: bool = True,
) -> str | None:
    import os

    direct = options.get(key)
    if direct is not None:
        if not isinstance(direct, str) or not direct:
            raise ValueError(f"sink option {key!r} must be a non-empty string")
        return direct

    env_name = options.get(f"{key}_env", env_key)
    if not isinstance(env_name, str) or not env_name:
        raise ValueError(f"sink option {key + '_env'!r} must be a non-empty string")
    value = os.environ.get(env_name)
    if value:
        return value
    if required:
        raise ValueError(
            f"sink option {key!r} is missing; set {env_name!r} "
            f"or sinks.options.{key}"
        )
    return None


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

    def databricks_factory(options: dict[str, object], base_dir: Path):
        from databricks import sql as databricks_sql

        from .databricks import DatabricksAnalyticalSink

        server_hostname = _option_or_env(
            options,
            "server_hostname",
            "DATABRICKS_SERVER_HOSTNAME",
        )
        http_path = _option_or_env(
            options,
            "http_path",
            "DATABRICKS_HTTP_PATH",
        )
        access_token = _option_or_env(
            options,
            "access_token",
            "DATABRICKS_TOKEN",
        )
        events_table = options.get("events_table", "sose_events")
        batches_table = options.get("batches_table", "sose_batches")
        if not isinstance(events_table, str) or not isinstance(batches_table, str):
            raise ValueError("Databricks table names must be strings")

        return DatabricksAnalyticalSink(
            lambda: databricks_sql.connect(
                server_hostname=server_hostname,
                http_path=http_path,
                access_token=access_token,
            ),
            events_table=events_table,
            batches_table=batches_table,
        )

    registry.register(
        SinkAdapter(
            name="databricks",
            factory=databricks_factory,
            optional_extra="databricks",
        )
    )

    def snowflake_factory(options: dict[str, object], base_dir: Path):
        import snowflake.connector

        from .snowflake import SnowflakeAnalyticalSink

        connection_name = options.get("connection_name")
        if connection_name is not None:
            if not isinstance(connection_name, str) or not connection_name:
                raise ValueError("Snowflake connection_name must be a non-empty string")

            def connect():
                return snowflake.connector.connect(
                    connection_name=connection_name,
                )
        else:
            account = _option_or_env(options, "account", "SNOWFLAKE_ACCOUNT")
            user = _option_or_env(options, "user", "SNOWFLAKE_USER")
            password = _option_or_env(options, "password", "SNOWFLAKE_PASSWORD")
            warehouse = _option_or_env(
                options,
                "warehouse",
                "SNOWFLAKE_WAREHOUSE",
                required=False,
            )
            database = _option_or_env(
                options,
                "database",
                "SNOWFLAKE_DATABASE",
                required=False,
            )
            schema = _option_or_env(
                options,
                "schema",
                "SNOWFLAKE_SCHEMA",
                required=False,
            )
            role = _option_or_env(
                options,
                "role",
                "SNOWFLAKE_ROLE",
                required=False,
            )

            def connect():
                kwargs = {
                    "account": account,
                    "user": user,
                    "password": password,
                }
                if warehouse:
                    kwargs["warehouse"] = warehouse
                if database:
                    kwargs["database"] = database
                if schema:
                    kwargs["schema"] = schema
                if role:
                    kwargs["role"] = role
                return snowflake.connector.connect(**kwargs)

        events_table = options.get("events_table", "SOSE_EVENTS")
        batches_table = options.get("batches_table", "SOSE_BATCHES")
        if not isinstance(events_table, str) or not isinstance(batches_table, str):
            raise ValueError("Snowflake table names must be strings")

        return SnowflakeAnalyticalSink(
            connect,
            events_table=events_table,
            batches_table=batches_table,
        )

    registry.register(
        SinkAdapter(
            name="snowflake",
            factory=snowflake_factory,
            optional_extra="snowflake",
        )
    )
    return registry
