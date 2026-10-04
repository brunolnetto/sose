import tempfile
from pathlib import Path

from sose.persistence.duckdb import DuckDBPersistence
from tests.support.persistence_conformance import PersistenceConformanceSuite


class TestDuckDBPersistenceConformance(PersistenceConformanceSuite):
    def make_persistence(self):
        directory = tempfile.mkdtemp(prefix="sose-duckdb-")
        return DuckDBPersistence(Path(directory) / "sose.duckdb")
