import os
import tempfile

from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence
from tests.support.persistence_conformance import PersistenceConformanceSuite


class TestSQLiteIncrementalPersistenceConformance(PersistenceConformanceSuite):
    def make_persistence(self):
        fd, path = tempfile.mkstemp(prefix="sose-incremental-", suffix=".sqlite3")
        os.close(fd)
        return SQLiteIncrementalPersistence(path)
