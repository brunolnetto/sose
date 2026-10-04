from datetime import datetime, timezone

from sose.domain.entity import Entity
from sose.persistence.memory import MemoryPersistence


def test_transaction_commits_entity_and_tick():
    store = MemoryPersistence()
    entity = Entity("1", "demo", created_at=datetime.now(timezone.utc))

    with store.transaction() as uow:
        uow.save_entity(entity)
        uow.set_committed_tick(7)

    assert store.entity("demo", "1") is not None
    assert store.committed_tick() == 7
