from sose.persistence.base import Persistence, UnitOfWork
from sose.persistence.engine import (
    EnginePersistence, EnginePersistenceCapabilities,
    EnginePersistenceRegistry, EngineUnitOfWork,
)
from sose.persistence.registry import PersistenceCapabilities, PersistenceRegistry


def test_engine_oltp_names_preserve_existing_contract_compatibility():
    assert EnginePersistence is Persistence
    assert EngineUnitOfWork is UnitOfWork
    assert EnginePersistenceCapabilities is PersistenceCapabilities
    assert EnginePersistenceRegistry is PersistenceRegistry
