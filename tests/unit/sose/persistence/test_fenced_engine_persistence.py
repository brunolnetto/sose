from contextlib import contextmanager

import pytest

from sose.persistence.ownership import FencedEnginePersistence


class Lease:
    owner_id="worker-a"
    epoch=7

class Persistence:
    def __init__(self): self.seen=[]
    @contextmanager
    def transaction(self, *, owner_epoch=None):
        self.seen.append(owner_epoch)
        yield object()
    def marker(self): return "ok"


def test_fenced_engine_persistence_injects_epoch_and_delegates_reads():
    raw=Persistence(); fenced=FencedEnginePersistence(raw, Lease())
    with fenced.transaction(): pass
    assert raw.seen==[7]
    assert fenced.marker()=="ok"

def test_fenced_view_does_not_expose_tokenless_transaction_path():
    raw=Persistence(); fenced=FencedEnginePersistence(raw, Lease())
    with fenced.transaction(): pass
    with fenced.transaction(): pass
    assert raw.seen==[7,7]
