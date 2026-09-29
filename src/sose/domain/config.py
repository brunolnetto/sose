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
    runtime_mutable_fields: frozenset[str] = frozenset(
        {"tick_step", "random_seed"}
    )

    def __post_init__(self) -> None:
        known = set(self.config_model.model_fields)
        unknown = set(self.runtime_mutable_fields) - known
        if unknown:
            raise ValueError(
                f"runtime mutable fields are not in {self.config_model.__name__}: "
                f"{sorted(unknown)}"
            )
        if "start_at" in self.runtime_mutable_fields:
            raise ValueError("start_at cannot be runtime mutable")

    def default_config(self) -> ConfigT:
        return self.config_model()

    def parse_config(self, value: ConfigT | dict[str, object] | None = None) -> ConfigT:
        if value is None:
            return self.default_config()
        if isinstance(value, self.config_model):
            return value
        return self.config_model.model_validate(value)

    def changed_config_fields(
        self,
        before: ConfigT,
        after: ConfigT,
    ) -> frozenset[str]:
        return frozenset(
            name
            for name in self.config_model.model_fields
            if getattr(before, name) != getattr(after, name)
        )

    def validate_runtime_config_change(
        self,
        before: ConfigT,
        after: ConfigT,
    ) -> frozenset[str]:
        changed = self.changed_config_fields(before, after)
        blocked = changed - self.runtime_mutable_fields
        if blocked:
            raise ValueError(
                "configuration fields are bootstrap-only after initialization: "
                + ", ".join(sorted(blocked))
            )
        return changed

    def describe_config(self) -> dict[str, object]:
        """Return JSON-serializable parameter metadata for discovery/UIs."""

        # Discovery describes values that callers may submit back through
        # model_validate(), so use Pydantic's validation-side aliases for the
        # schema. Serialization aliases can intentionally differ.
        schema = self.config_model.model_json_schema(
            mode="validation",
            by_alias=True,
        )
        required = set(schema.get("required", ()))
        properties = schema.get("properties", {})

        # model_dump(by_alias=True) follows serialization aliases and therefore
        # cannot be joined directly to a validation-mode schema. Preserve field
        # order and pair each schema property with the field's Python attribute
        # value instead. Pydantic emits model properties in model-field order.
        instance = self.default_config()
        model_field_names = tuple(self.config_model.model_fields)
        property_names = tuple(properties)
        if len(model_field_names) != len(property_names):
            raise RuntimeError(
                "domain config discovery cannot align model fields with JSON schema"
            )
        field_defaults = instance.model_dump(mode="json")
        field_to_property = dict(
            zip(model_field_names, property_names, strict=True)
        )
        property_to_field = {
            property_name: field_name
            for field_name, property_name in field_to_property.items()
        }
        defaults = {
            property_name: field_defaults[field_name]
            for field_name, property_name in field_to_property.items()
        }

        parameters: list[dict[str, object]] = []
        for name, field_schema in properties.items():
            field_name = property_to_field[name]
            parameters.append(
                {
                    "name": name,
                    "field_name": field_name,
                    "required": name in required,
                    "default": defaults.get(name),
                    "mutability": (
                        "runtime"
                        if field_name in self.runtime_mutable_fields
                        else "bootstrap"
                    ),
                    "schema": field_schema,
                }
            )

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
