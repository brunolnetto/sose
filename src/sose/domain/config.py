from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.persistence.base import Persistence


class DomainConfig(BaseModel):
    """Validated, serializable configuration owned by one domain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_at: datetime
    tick_step: timedelta = Field(default=timedelta(hours=1), gt=timedelta(0))
    random_seed: int = 42


ConfigT = TypeVar("ConfigT", bound=DomainConfig)
SeedT = TypeVar("SeedT")


BuildRuntime = Callable[
    [Persistence, ConfigT, datetime, int],
    tuple[SimulationContext, Engine],
]
SeedDomain = Callable[[Persistence, ConfigT], SeedT]


@dataclass(frozen=True, slots=True)
class DomainDefinition(Generic[ConfigT, SeedT]):
    """High-level executable domain contract.

    Domain-specific meaning remains inside the domain. This descriptor only
    standardizes validated configuration, runtime construction, and seeding so
    jobs/CLI code do not need bespoke imports or keyword conventions.
    """

    name: str
    description: str
    config_model: type[ConfigT]
    build_runtime: Callable[
        [Persistence, ConfigT, datetime, int],
        tuple[SimulationContext, Engine],
    ]
    seed: Callable[[Persistence, ConfigT], SeedT]
    reconcile_tick: Callable[
        [Persistence, Engine, object, ConfigT, SeedT],
        None,
    ] | None = None

    def default_config(self) -> ConfigT:
        return self.config_model()

    def parse_config(self, value: ConfigT | dict[str, object] | None = None) -> ConfigT:
        if value is None:
            return self.default_config()
        if isinstance(value, self.config_model):
            return value
        return self.config_model.model_validate(value)

    def describe_config(self) -> dict[str, object]:
        """Return JSON-serializable parameter metadata for discovery/UIs."""

        schema = self.config_model.model_json_schema()
        defaults = self.default_config().model_dump(mode="json")
        required = set(schema.get("required", ()))
        properties = schema.get("properties", {})

        parameters: list[dict[str, object]] = []
        for name, field_schema in properties.items():
            item = {
                "name": name,
                "required": name in required,
                "default": defaults.get(name),
                "schema": field_schema,
            }
            parameters.append(item)

        return {
            "name": self.name,
            "description": self.description,
            "config_model": self.config_model.__name__,
            "defaults": defaults,
            "parameters": parameters,
            "$defs": schema.get("$defs", {}),
        }


class DomainCatalog:
    def __init__(self) -> None:
        self._definitions: dict[str, DomainDefinition] = {}

    def register(self, definition: DomainDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"domain already registered: {definition.name}")
        self._definitions[definition.name] = definition

    def get(self, name: str) -> DomainDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise KeyError(f"unknown domain: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def definitions(self) -> tuple[DomainDefinition, ...]:
        return tuple(self._definitions[name] for name in self.names())
