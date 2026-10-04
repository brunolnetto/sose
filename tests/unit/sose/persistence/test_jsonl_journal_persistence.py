import tempfile
from pathlib import Path

from sose.persistence.jsonl_journal import JSONLJournalPersistence
from tests.support.persistence_conformance import PersistenceConformanceSuite


class TestJSONLJournalPersistenceConformance(PersistenceConformanceSuite):
    def make_persistence(self):
        directory = tempfile.mkdtemp(prefix="sose-journal-")
        return JSONLJournalPersistence(Path(directory) / "sose.jsonl")
