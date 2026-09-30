from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sose.jobs.config import SOSEConfig
from sose.persistence.registry import (
    PersistenceAdapter,
    PersistenceRegistry,
    builtin_persistence_registry,
)
from sose.sinks.base import SinkBinding
from sose.sinks.registry import (
    SinkAdapter,
    SinkRegistry,
    builtin_sink_registry,
)


@dataclass(frozen=True, slots=True)
class StorageIssue:
    code: str
    message: str
    severity: str = "warning"


@dataclass(frozen=True, slots=True)
class AuthoritativeStoragePlan:
    adapter: PersistenceAdapter
    required_capabilities: tuple[str, ...]
    options: dict[str, object]

    @property
    def capabilities(self) -> tuple[str, ...]:
        return self.adapter.capabilities.names()

    @property
    def durable_recurring_ready(self) -> bool:
        caps = self.adapter.capabilities
        return caps.process_durable and caps.transactional_commits


@dataclass(frozen=True, slots=True)
class AnalyticalStoragePlan:
    name: str
    adapter: SinkAdapter
    options: dict[str, object]


@dataclass(frozen=True, slots=True)
class StoragePlan:
    authoritative: AuthoritativeStoragePlan
    analytical: tuple[AnalyticalStoragePlan, ...]
    issues: tuple[StorageIssue, ...]

    @property
    def healthy(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def describe(self) -> dict[str, object]:
        return {
            "authoritative": {
                "role": "authoritative",
                "adapter": self.authoritative.adapter.name,
                "optional_extra": self.authoritative.adapter.optional_extra,
                "required_capabilities": list(
                    self.authoritative.required_capabilities
                ),
                "capabilities": list(self.authoritative.capabilities),
                "durable_recurring_ready": (
                    self.authoritative.durable_recurring_ready
                ),
            },
            "analytical": [
                {
                    "role": "analytical",
                    "name": item.name,
                    "adapter": item.adapter.name,
                    "optional_extra": item.adapter.optional_extra,
                }
                for item in self.analytical
            ],
            "issues": [
                {
                    "code": issue.code,
                    "message": issue.message,
                    "severity": issue.severity,
                }
                for issue in self.issues
            ],
        }

    def create_authoritative(
        self,
        *,
        registry: PersistenceRegistry,
        base_dir: Path,
    ):
        return registry.create(
            self.authoritative.adapter.name,
            self.authoritative.options,
            base_dir=base_dir,
        )

    def create_sink_bindings(
        self,
        *,
        registry: SinkRegistry,
        base_dir: Path,
    ) -> tuple[SinkBinding, ...]:
        return tuple(
            SinkBinding(
                name=item.name,
                sink=registry.create(
                    item.adapter.name,
                    item.options,
                    base_dir=base_dir,
                ),
            )
            for item in self.analytical
        )


def build_storage_plan(
    config: SOSEConfig,
    *,
    persistence_registry: PersistenceRegistry | None = None,
    sink_registry: SinkRegistry | None = None,
) -> StoragePlan:
    """Resolve one job's authoritative store and optional analytical sinks."""

    persistence_adapters = (
        persistence_registry or builtin_persistence_registry()
    )
    sink_adapters = sink_registry or builtin_sink_registry()

    authoritative_adapter = persistence_adapters.require(
        config.persistence.adapter,
        *config.persistence.require,
    )

    seen_sink_names: set[str] = set()
    analytical: list[AnalyticalStoragePlan] = []
    for section in config.sinks:
        if section.name in seen_sink_names:
            raise ValueError(
                f"duplicate analytical sink name: {section.name}"
            )
        seen_sink_names.add(section.name)
        analytical.append(
            AnalyticalStoragePlan(
                name=section.name,
                adapter=sink_adapters.adapter(section.adapter),
                options=dict(section.options),
            )
        )

    authoritative = AuthoritativeStoragePlan(
        adapter=authoritative_adapter,
        required_capabilities=tuple(config.persistence.require),
        options=dict(config.persistence.options),
    )

    issues: list[StorageIssue] = []
    if not authoritative.durable_recurring_ready:
        issues.append(
            StorageIssue(
                code="storage.authoritative_not_process_durable",
                message=(
                    f"authoritative adapter {authoritative.adapter.name!r} "
                    "does not provide process-durable recurring job state"
                ),
            )
        )

    if authoritative.adapter.capabilities.append_only:
        issues.append(
            StorageIssue(
                code="storage.authoritative_append_only",
                message=(
                    f"authoritative adapter {authoritative.adapter.name!r} "
                    "uses append-oriented storage; verify replay growth for "
                    "long-lived recurring jobs"
                ),
            )
        )

    return StoragePlan(
        authoritative=authoritative,
        analytical=tuple(analytical),
        issues=tuple(issues),
    )
