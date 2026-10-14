"""Pickup admission must use the organization's own durable recovery clock."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sose.composition.trading_company_customer import pickup_resource_due_at
from sose.core.events import Command
from sose.core.runtime import SimulationPosition


ORIGIN = datetime(2026, 3, 1, 8, tzinfo=timezone.utc)


class _TwoOrganizationStore:
    def __init__(self):
        self.read_owners = []

    def entity(self, entity_type, entity_id):
        assert (entity_type, entity_id) == ("shipment", "shipment-1")
        return SimpleNamespace(state="created")

    def simulation_position(self, *, owner_id):
        self.read_owners.append(owner_id)
        ticks = {"org-a": 0, "org-b": 6}
        if owner_id not in ticks:
            raise AssertionError("unexpected operational owner")
        return SimulationPosition(
            logical_time=ORIGIN + timedelta(minutes=5 * ticks[owner_id]),
            logical_tick=ticks[owner_id],
            execution_sequence=ticks[owner_id],
            committed_sequence=ticks[owner_id],
        )


def _pickup(owner_id: str):
    return Command(
        command_id=f"pickup-{owner_id}",
        name="composition.deliver_shipment",
        entity_type="shipment",
        entity_id="shipment-1",
        due_at=ORIGIN,
        owner_id=owner_id,
    )


def test_pickup_due_time_is_isolated_by_organization():
    store = _TwoOrganizationStore()
    due_a = pickup_resource_due_at(store, _pickup("org-a"), organization_id="org-a")
    due_b = pickup_resource_due_at(store, _pickup("org-b"), organization_id="org-b")

    assert due_a == ORIGIN + timedelta(hours=1)
    assert due_b == ORIGIN + timedelta(hours=1, minutes=30)
    assert store.read_owners == ["org-a", "org-b"]


@pytest.mark.parametrize("owner_id", ["", " ", None])
def test_pickup_rejects_missing_organization_before_read(owner_id):
    store = _TwoOrganizationStore()
    with pytest.raises(ValueError, match="organization_id must not be empty"):
        pickup_resource_due_at(store, _pickup("org-a"), organization_id=owner_id)
    assert store.read_owners == []
