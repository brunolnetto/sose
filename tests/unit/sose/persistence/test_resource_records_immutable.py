"""#610: committed reads return the stored resource records without a deep copy.

That is only safe while every resource record is deeply immutable: a frozen
dataclass whose fields are immutable scalars. These tests fail first if a mutable
field (a list, a dict, a mutable object) is ever added to one of them.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
import typing

import pytest

from sose.core.runtime import (
    ResourceDefinition,
    ResourceDemand,
    ResourceReleaseIntent,
    ResourceReservation,
)
from sose.persistence.memory import MemoryPersistence

# ``str | None``: the optional owner of a resource queue record (#496) is immutable too.
IMMUTABLE_FIELD_TYPES = {str, int, datetime, str | None, typing.Optional[str]}
RECORDS = (ResourceDefinition, ResourceDemand, ResourceReservation, ResourceReleaseIntent)
NOW = datetime(2026, 11, 21, 8, tzinfo=timezone.utc)


@pytest.mark.parametrize("record", RECORDS, ids=lambda record: record.__name__)
def test_resource_record_is_a_frozen_dataclass_of_immutable_scalars(record):
    assert dataclasses.is_dataclass(record)
    assert record.__dataclass_params__.frozen
    hints = typing.get_type_hints(record)
    for field in dataclasses.fields(record):
        assert hints[field.name] in IMMUTABLE_FIELD_TYPES, (record.__name__, field.name)


def test_reads_share_records_and_cannot_change_committed_state():
    store = MemoryPersistence()
    reservation = ResourceReservation("reservation-1", "request-1", "pool", NOW, 1)
    with store.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("pool", capacity=1))
        uow.save_resource_reservation(reservation)
        uow.save_resource_demand(ResourceDemand("request-2", "pool", 5, NOW, 2))
    [read] = store.resource_reservations()
    assert read == reservation
    with pytest.raises(dataclasses.FrozenInstanceError):
        read.request_id = "changed"                     # type: ignore[misc]
    assert store.resource_reservations() == (reservation,)
    # A rolled-back transaction leaves the committed records untouched.
    with pytest.raises(RuntimeError):
        with store.transaction() as uow:
            uow.delete_resource_reservation("reservation-1")
            uow.delete_resource_demand("request-2")
            raise RuntimeError("roll back")
    assert store.resource_reservations() == (reservation,)
    assert [demand.request_id for demand in store.resource_demands()] == ["request-2"]
