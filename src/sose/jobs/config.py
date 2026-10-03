from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re
import tomllib

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


_ENGINE_NAMESPACE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")


def engine_namespace_for_job(job_id: str) -> str:
    """Return a stable SQL-safe Engine Store namespace for one durable job."""
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", job_id).strip("_").lower()
    if not normalized or normalized[0].isdigit():
        normalized = f"job_{normalized}"
    digest = sha256(job_id.encode("utf-8")).hexdigest()[:8]
    stem = normalized[:30]
    namespace = f"{stem}_{digest}"
    if not _ENGINE_NAMESPACE_RE.fullmatch(namespace):  # pragma: no cover - construction invariant
        raise ValueError(f"cannot derive Engine Store namespace from job id: {job_id!r}")
    return namespace


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


class CatalogJobSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    domain: DomainSection
    domain_store: DomainWarehouseSection | None = Field(
        default=None,
        validation_alias=AliasChoices("domain_store", "domain_warehouse"),
    )
    sinks: list[SinkSection] = Field(default_factory=list)
    ticks_per_trigger: int = Field(default=1, ge=1)
    max_ticks_per_trigger: int = Field(default=100, ge=1)
    engine_namespace: str | None = None

    @model_validator(mode="after")
    def validate_catalog_job(self) -> "CatalogJobSection":
        if not self.id:
            raise ValueError("catalog job id cannot be empty")
        if self.ticks_per_trigger > self.max_ticks_per_trigger:
            raise ValueError(
                "ticks_per_trigger cannot exceed max_ticks_per_trigger"
            )
        if (
            self.engine_namespace is not None
            and not _ENGINE_NAMESPACE_RE.fullmatch(self.engine_namespace)
        ):
            raise ValueError(
                "engine_namespace must be a SQL-safe identifier "
                "with at most 40 characters"
            )
        return self

    @property
    def resolved_engine_namespace(self) -> str:
        return self.engine_namespace or engine_namespace_for_job(self.id)


class SOSECatalogConfig(BaseModel):
    """Multiple durable jobs sharing one physical Engine Store configuration."""

    model_config = ConfigDict(extra="forbid")

    engine_store: PersistenceSection = Field(default_factory=PersistenceSection)
    runtime: RuntimeSection = Field(default_factory=RuntimeSection)
    jobs: list[CatalogJobSection] = Field(min_length=1)

    def _validate_unique_ids(self) -> None:
        ids = [job.id for job in self.jobs]
        if len(ids) != len(set(ids)):
            raise ValueError("catalog job ids must be unique")

    def _validate_unique_namespaces(self) -> None:
        namespaces = [job.resolved_engine_namespace for job in self.jobs]
        if len(namespaces) != len(set(namespaces)):
            raise ValueError("catalog Engine Store namespaces must be unique")

    def _validate_engine_store(self) -> None:
        if self.engine_store.adapter not in {"sqlite_incremental", "postgres"}:
            raise ValueError(
                "shared Engine Store catalogs currently require "
                "'sqlite_incremental' or 'postgres'"
            )
        if "namespace" in self.engine_store.options:
            raise ValueError(
                "catalog Engine Store namespace is job-specific; "
                "set jobs[].engine_namespace instead"
            )
        if (
            self.engine_store.adapter == "sqlite_incremental"
            and self.engine_store.options.get("path") == ":memory:"
        ):
            raise ValueError(
                "shared SQLite Engine Store catalogs require a file-backed path"
            )

    def _validate_sqlite_namespaces_lowercase(self) -> None:
        if self.engine_store.adapter != "sqlite_incremental":
            return
        noncanonical = [
            job.engine_namespace
            for job in self.jobs
            if (
                job.engine_namespace is not None
                and job.engine_namespace != job.engine_namespace.lower()
            )
        ]
        if noncanonical:
            raise ValueError(
                "SQLite catalog engine_namespace values must be lowercase"
            )

    @model_validator(mode="after")
    def validate_unique_jobs(self) -> "SOSECatalogConfig":
        self._validate_unique_ids()
        self._validate_unique_namespaces()
        self._validate_engine_store()
        self._validate_sqlite_namespaces_lowercase()
        return self


def load_sose_catalog_config(
    path: str | Path,
) -> tuple[SOSECatalogConfig, Path]:
    config_path = Path(path).resolve()
    payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    return SOSECatalogConfig.model_validate(payload), config_path.parent
