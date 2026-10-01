from __future__ import annotations

from pathlib import Path
import tomllib

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class DomainSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    parameters: dict[str, object] = Field(default_factory=dict)


class PersistenceSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter: str = "sqlite_incremental"
    require: list[str] = Field(default_factory=list)
    options: dict[str, object] = Field(default_factory=dict)


class DomainWarehouseSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter: str = "sqlite"
    options: dict[str, object] = Field(default_factory=dict)


class SinkSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    adapter: str
    options: dict[str, object] = Field(default_factory=dict)


class RuntimeSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: str = "simpy"


class JobSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    ticks_per_trigger: int = Field(default=1, ge=1)
    max_ticks_per_trigger: int = Field(default=100, ge=1)

    @model_validator(mode="after")
    def validate_trigger_bounds(self) -> "JobSection":
        if self.ticks_per_trigger > self.max_ticks_per_trigger:
            raise ValueError(
                "ticks_per_trigger cannot exceed max_ticks_per_trigger"
            )
        return self


class SOSEConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    domain: DomainSection
    engine_store: PersistenceSection = Field(
        default_factory=PersistenceSection,
        validation_alias=AliasChoices("engine_store", "persistence"),
    )
    domain_store: DomainWarehouseSection | None = Field(
        default=None,
        validation_alias=AliasChoices("domain_store", "domain_warehouse"),
    )
    runtime: RuntimeSection = Field(default_factory=RuntimeSection)

    @property
    def persistence(self) -> PersistenceSection:
        """Compatibility alias for the former public configuration name."""
        return self.engine_store

    @property
    def domain_warehouse(self) -> DomainWarehouseSection | None:
        """Compatibility alias for the former public configuration name."""
        return self.domain_store
    sinks: list[SinkSection] = Field(default_factory=list)
    job: JobSection


def load_sose_config(path: str | Path) -> tuple[SOSEConfig, Path]:
    config_path = Path(path).resolve()
    payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    return SOSEConfig.model_validate(payload), config_path.parent
