"""Owner-scoped diagnostics must never report another organization's causal events."""
from datetime import datetime, timezone

import pytest

from sose.core.diagnostics import collect_runtime_diagnostics
from sose.core.events import DomainEvent
from sose.persistence.memory import MemoryPersistence


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_diagnostics_include_only_events_owned_by_selected_organization():
    store = MemoryPersistence()
    with store.transaction() as uow:
        for owner, suffix in (("organization-a", "a"), ("organization-b", "b")):
            uow.append_event(DomainEvent(
                event_id="event-" + suffix, name="transition",
                entity_type="shipment", entity_id="same-type-" + suffix,
                occurred_at=NOW, owner_id=owner,
            ))
    assert collect_runtime_diagnostics(store, owner_id="organization-a").counts.events == 1
    assert collect_runtime_diagnostics(store, owner_id="organization-b").counts.events == 1


def test_diagnostics_reject_missing_or_blank_ownership():
    store = MemoryPersistence()
    with pytest.raises(TypeError, match="owner_id"):
        collect_runtime_diagnostics(store)
    for invalid in (None, "", "  "):
        with pytest.raises(ValueError, match="owner_id"):
            collect_runtime_diagnostics(store, owner_id=invalid)
