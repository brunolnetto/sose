from __future__ import annotations

from pathlib import Path
import tomllib

from pydantic import BaseModel, ConfigDict, Field


class DomainSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    parameters: dict[str, object] = Field(default_factory=dict)


class PersistenceSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter: str = "sqlite_incremental"
    options: dict[str, object] = Field(default_factory=dict)


class RuntimeSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: str = "simpy"


class JobSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    ticks_per_trigger: int = Field(default=1, ge=1)
    max_ticks_per_trigger: int = Field(default=100, ge=1)


class SOSEConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: DomainSection
    persistence: PersistenceSection = Field(default_factory=PersistenceSection)
    runtime: RuntimeSection = Field(default_factory=RuntimeSection)
    job: JobSection


def load_sose_config(path: str | Path) -> tuple[SOSEConfig, Path]:
    config_path = Path(path).resolve()
    payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    return SOSEConfig.model_validate(payload), config_path.parent
