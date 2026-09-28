import os
import tempfile

from sose.persistence.sqlite import SQLitePersistence
from tests.support.persistence_conformance import PersistenceConformanceSuite


class TestSQLitePersistenceConformance(PersistenceConformanceSuite):
    def make_persistence(self):
        fd, path = tempfile.mkstemp(prefix="sose-conformance-", suffix=".sqlite3")
        os.close(fd)
        return SQLitePersistence(path)
