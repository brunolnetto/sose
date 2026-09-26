from sose.persistence.memory import MemoryPersistence
from tests.support.persistence_conformance import PersistenceConformanceSuite


class TestMemoryPersistenceConformance(PersistenceConformanceSuite):
    def make_persistence(self):
        return MemoryPersistence()
