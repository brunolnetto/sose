import os
import tempfile

import pytest

from sose.persistence.sqlite import SQLitePersistence
from tests.support.persistence_conformance import PersistenceConformanceSuite


class TestSQLitePersistenceConformance(PersistenceConformanceSuite):
    @pytest.fixture(autouse=True)
    def _close_persistences(self):
        self._stores: list[SQLitePersistence] = []
        yield
        for store in self._stores:
            store.close()

    def make_persistence(self):
        fd, path = tempfile.mkstemp(prefix="sose-conformance-", suffix=".sqlite3")
        os.close(fd)
        store = SQLitePersistence(path)
        self._stores.append(store)
        return store
