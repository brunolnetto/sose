"""Falsify actor/calendar conservation near nominal capacity boundaries."""
import math

import pytest
from pydantic import ValidationError

from sose.organizational.ledger import (
    ActorCategory, ActorLedger, ActorLedgerInterval,
    ItemCategory, ItemLedger, ItemLedgerInterval,
)


def item(start, end, category=ItemCategory.PROCESSING):
    return ItemLedgerInterval(start=start, end=end, primary=category)


def actor(start, end, category=ActorCategory.EXECUTION, allocation=1):
    return ActorLedgerInterval(
        start=start, end=end, category=category, allocation=allocation,
    )


@pytest.mark.parametrize("start,end", [(1, 1), (2, 1)])
def test_zero_and_negative_item_intervals_are_invalid(start, end):
    with pytest.raises(ValidationError, match="end must be greater"):
        item(start, end)


@pytest.mark.parametrize("start,end", [(1, 1), (4, 3)])
def test_zero_and_negative_actor_intervals_are_invalid(start, end):
    with pytest.raises(ValidationError, match="end must be greater"):
        actor(start, end)


def test_item_ledger_empty_boundary_and_elapsed_time():
    empty = ItemLedger(work_item_id="empty")
    assert empty.elapsed == 0
    assert empty.duration_by_primary() == {}
    empty.assert_complete(created_at=4, observed_at=4)
    with pytest.raises(ValueError, match="does not cover"):
        empty.assert_complete(created_at=4, observed_at=5)
    with pytest.raises(ValueError, match="precede"):
        empty.assert_complete(created_at=5, observed_at=4)
    for bad in (math.nan, math.inf):
        with pytest.raises(ValueError, match="finite"):
            empty.assert_complete(created_at=bad, observed_at=5)
        with pytest.raises(ValueError, match="finite"):
            empty.assert_complete(created_at=4, observed_at=bad)


def test_item_ledger_missing_observation_boundary_and_primary_duration():
    ledger = ItemLedger(work_item_id="item", intervals=(
        item(0, 1, ItemCategory.QUEUE), item(1, 2, ItemCategory.QUEUE),
        item(2, 3, ItemCategory.PROCESSING),
    ))
    assert ledger.duration_by_primary() == {
        ItemCategory.QUEUE: 2, ItemCategory.PROCESSING: 1,
    }
    ledger.assert_complete(created_at=0, observed_at=3)
    for beginning, ending in ((-1, 3), (0, 4)):
        with pytest.raises(ValueError, match="does not cover"):
            ledger.assert_complete(created_at=beginning, observed_at=ending)


def test_fractional_allocations_reject_idle_or_unavailable_overlap():
    for unavailable in (ActorCategory.IDLE, ActorCategory.UNAVAILABLE):
        with pytest.raises(ValueError, match="cannot overlap"):
            ActorLedger(actor_id="mixed", fractional_multitasking=True, intervals=(
                actor(0, 3, unavailable),
                actor(1, 2, ActorCategory.EXECUTION, allocation=0.1),
            ))
        with pytest.raises(ValidationError, match="require allocation=1"):
            actor(0, 3, unavailable, allocation=0.25)


def test_fractional_actor_can_share_one_unit_and_never_hide_gaps():
    ledger = ActorLedger(actor_id="one", fractional_multitasking=True, intervals=(
        actor(0, 2, allocation=0.25), actor(0, 2, allocation=0.75),
        actor(2, 3, ActorCategory.IDLE),
        actor(3, 4, ActorCategory.UNAVAILABLE),
    ))
    assert ledger.allocated_actor_time == 2
    assert ledger.idle_time == 1
    assert ledger.unavailable_time == 1
    assert ledger.available_time == 3
    assert ledger.utilization == pytest.approx(2 / 3)
    assert ledger.duration_by_category()[ActorCategory.EXECUTION] == 2
    ledger.assert_complete(start=0, end=4)
    with pytest.raises(ValueError, match="does not cover"):
        ledger.assert_complete(start=0, end=5)

    sparse = ActorLedger(actor_id="sparse", intervals=(
        actor(0, 1), actor(2, 3),
    ))
    with pytest.raises(ValueError, match="conservation"):
        sparse.assert_complete(start=0, end=3)


def test_actor_empty_time_is_well_defined_not_utilized():
    empty = ActorLedger(actor_id="empty")
    assert empty.allocated_actor_time == 0
    assert empty.available_time == 0
    assert empty.utilization == 0
    assert empty.duration_by_category() == {}
    empty.assert_complete(start=7, end=7)
    with pytest.raises(ValueError, match="does not cover"):
        empty.assert_complete(start=7, end=8)
    with pytest.raises(ValueError, match="precede"):
        empty.assert_complete(start=8, end=7)
    for bad in (math.inf, math.nan):
        with pytest.raises(ValueError, match="finite"):
            empty.assert_complete(start=bad, end=8)
        with pytest.raises(ValueError, match="finite"):
            empty.assert_complete(start=7, end=bad)


def test_fractional_overcommit_rejected_even_with_different_actor_categories():
    with pytest.raises(ValueError, match="exceeds actor capacity"):
        ActorLedger(actor_id="over", fractional_multitasking=True, intervals=(
            actor(0, 2, ActorCategory.EXECUTION, 0.6),
            actor(1, 3, ActorCategory.GOVERNANCE, 0.5),
        ))


def test_union_of_adjacent_and_overlapping_calendar_intervals():
    ledger = ActorLedger(actor_id="calendar", fractional_multitasking=True, intervals=(
        actor(0, 3, ActorCategory.EXECUTION, 0.3),
        actor(1, 5, ActorCategory.ADAPTATION, 0.3),
        actor(5, 6, ActorCategory.IDLE),
    ))
    assert ledger.available_time == 6
    ledger.assert_complete(start=0, end=6)
