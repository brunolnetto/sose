"""Stable domain-author API for SOSE.

The symbols exported here are the compatibility-relevant surface for domain
authors during the v0.8 stabilization line. Lower-level modules remain
importable, but are classified separately in docs/architecture/public-api.md.
"""

from sose.backends.base import (
    ContainerBackend,
    PreemptiveResourceBackend,
    ResourceBackend,
    StoreBackend,
    TemporalBackend,
)
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.diagnostics import (
    DiagnosticIssue,
    RuntimeCounts,
    RuntimeDiagnostics,
    collect_runtime_diagnostics,
)
from sose.core.engine import Engine
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.config import DomainCatalog, DomainConfig, DomainDefinition
from sose.domain.entity import Entity
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.base import Persistence, UnitOfWork
from sose.persistence.jsonl_journal import JSONLJournalPersistence
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence
from sose.jobs.config import SOSEConfig, load_sose_config
from sose.jobs.doctor import (
    JobDoctorIssue,
    JobDoctorReport,
    inspect_job_file_health,
    inspect_job_health,
)
from sose.jobs.factory import build_job_from_config, build_job_from_file
from sose.jobs.model import SimulationJobState
from sose.jobs.runner import JobTickResult, SimulationJob
from sose.jobs.scaffold import render_sose_toml, write_sose_toml
from sose.persistence.registry import (
    PersistenceAdapter,
    PersistenceCapabilities,
    PersistenceRegistry,
    builtin_persistence_registry,
)
from sose.probability import probabilistic, probabilistic_transitions
from sose.scenarios import (
    AttributeEffect,
    CompositeEffect,
    EventTrigger,
    Scenario,
    ScheduledTrigger,
    TickTrigger,
    TransitionWeightEffect,
)

__all__ = [
    "AttributeEffect",
    "CompositeEffect",
    "ContainerBackend",
    "DiagnosticIssue",
    "DomainCatalog",
    "DomainConfig",
    "DomainDefinition",
    "DomainRegistry",
    "Engine",
    "Entity",
    "EntityType",
    "EventTrigger",
    "JobDoctorIssue",
    "JobDoctorReport",
    "JobTickResult",
    "JSONLJournalPersistence",
    "MemoryPersistence",
    "Persistence",
    "PersistenceAdapter",
    "PersistenceCapabilities",
    "PersistenceRegistry",
    "PreemptiveResourceBackend",
    "RandomSource",
    "RuntimeCounts",
    "RuntimeDiagnostics",
    "ResourceBackend",
    "SQLiteIncrementalPersistence",
    "SQLitePersistence",
    "SOSEConfig",
    "Scenario",
    "Scheduler",
    "ScheduledTrigger",
    "SimulationClock",
    "SimulationJob",
    "SimulationJobState",
    "SimulationContext",
    "StoreBackend",
    "TemporalBackend",
    "TickTrigger",
    "TransitionWeightEffect",
    "UnitOfWork",
    "build_job_from_config",
    "build_job_from_file",
    "builtin_persistence_registry",
    "collect_runtime_diagnostics",
    "inspect_job_file_health",
    "inspect_job_health",
    "load_sose_config",
    "probabilistic",
    "probabilistic_transitions",
    "render_sose_toml",
    "write_sose_toml",
]
