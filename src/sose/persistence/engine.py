"""Engine OLTP persistence boundary.

This module names the persistence contract by responsibility: it stores SOSE
runtime/operational state required to resume deterministic execution. Domain
warehouse data belongs to a separate boundary and is intentionally absent here.

Persistence remains a compatibility alias while callers migrate.
"""

from __future__ import annotations

from typing import TypeAlias

from .base import Persistence, UnitOfWork
from .qualification import PersistenceQualification, PersistenceTier
from .registry import PersistenceAdapter, PersistenceCapabilities, PersistenceRegistry

EngineUnitOfWork: TypeAlias = UnitOfWork
EnginePersistence: TypeAlias = Persistence
EnginePersistenceAdapter: TypeAlias = PersistenceAdapter
EnginePersistenceCapabilities: TypeAlias = PersistenceCapabilities
EnginePersistenceQualification: TypeAlias = PersistenceQualification
EnginePersistenceRegistry: TypeAlias = PersistenceRegistry
EnginePersistenceTier: TypeAlias = PersistenceTier

__all__ = [
    "EngineUnitOfWork", "EnginePersistence", "EnginePersistenceAdapter",
    "EnginePersistenceCapabilities", "EnginePersistenceQualification",
    "EnginePersistenceRegistry", "EnginePersistenceTier",
]
