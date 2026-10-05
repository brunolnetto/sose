from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from sose.organizational.ledger import (
    ActorCategory,
    ActorLedger,
    ActorLedgerInterval,
    ItemCategory,
    ItemLedger,
    ItemLedgerInterval,
)


def test_item_interval_is_half_open_and_has_positive_duration() -> None:
    interval = ItemLedgerInterval(start=1.0, end=3.5, primary=ItemCategory.PROCESSING)
    assert interval.duration == 2.5
    assert interval.contains(1.0)
    assert interval.contains(3.499)
    assert not interval.contains(3.5)


def test_intervals_reject_non_finite_endpoints() -> None:
    with pytest.raises(ValidationError):
        ItemLedgerInterval(start=0, end=math.inf, primary=ItemCategory.PROCESSING)
    with pytest.raises(ValidationError):
        ItemLedgerInterval(start=math.nan, end=1, primary=ItemCategory.PROCESSING)
    with pytest.raises(ValidationError):
        ActorLedgerInterval(start=0, end=math.inf, category=ActorCategory.EXECUTION)
    with pytest.raises(ValidationError):
        ActorLedgerInterval(start=math.nan, end=1, category=ActorCategory.EXECUTION)


def test_item_ledger_requires_contiguous_non_overlapping_intervals() -> None:
    ledger = ItemLedger(
        work_item_id="item-1",
        intervals=[
            ItemLedgerInterval(start=0, end=1, primary=ItemCategory.QUEUE),
            ItemLedgerInterval(start=1, end=3, primary=ItemCategory.PROCESSING),
        ],
    )
    ledger.assert_complete(created_at=0, observed_at=3)
    assert ledger.elapsed == 3

    with pytest.raises(ValueError, match="gap or overlap"):
        ItemLedger(
            work_item_id="item-2",
            intervals=[
                ItemLedgerInterval(start=0, end=1, primary=ItemCategory.QUEUE),
                ItemLedgerInterval(start=2, end=3, primary=ItemCategory.PROCESSING),
            ],
        )


def test_ledger_snapshots_are_immutable_after_validation() -> None:
    item = ItemLedger(
        work_item_id="item-1",
        intervals=[ItemLedgerInterval(start=0, end=1, primary=ItemCategory.PROCESSING)],
    )
    actor = ActorLedger(
        actor_id="alice",
        intervals=[ActorLedgerInterval(start=0, end=1, category=ActorCategory.EXECUTION)],
    )
    assert isinstance(item.intervals, tuple)
    assert isinstance(actor.intervals, tuple)
    with pytest.raises(AttributeError):
        item.intervals.append(  # type: ignore[attr-defined]
            ItemLedgerInterval(start=1, end=2, primary=ItemCategory.PROCESSING)
        )
    with pytest.raises(AttributeError):
        actor.intervals.append(  # type: ignore[attr-defined]
            ActorLedgerInterval(start=1, end=2, category=ActorCategory.EXECUTION)
        )


def test_secondary_labels_do_not_add_item_duration() -> None:
    ledger = ItemLedger(
        work_item_id="item-1",
        intervals=[
            ItemLedgerInterval(
                start=0,
                end=2,
                primary=ItemCategory.WAITING_CALENDAR,
                secondary_labels=("decision", "authority:security"),
            )
        ],
    )
    assert ledger.duration_by_primary()[ItemCategory.WAITING_CALENDAR] == 2
    assert ledger.elapsed == 2


def test_actor_ledger_conserves_calendar_time() -> None:
    ledger = ActorLedger(
        actor_id="alice",
        intervals=[
            ActorLedgerInterval(start=9, end=12, category=ActorCategory.IDLE),
            ActorLedgerInterval(start=12, end=13, category=ActorCategory.UNAVAILABLE),
            ActorLedgerInterval(start=13, end=17, category=ActorCategory.EXECUTION),
        ],
    )
    ledger.assert_complete(start=9, end=17)
    assert ledger.available_time == 7
    assert ledger.utilization == pytest.approx(4 / 7)


def test_unavailable_time_is_not_idle_or_available() -> None:
    ledger = ActorLedger(
        actor_id="alice",
        intervals=[
            ActorLedgerInterval(start=0, end=1, category=ActorCategory.UNAVAILABLE),
            ActorLedgerInterval(start=1, end=2, category=ActorCategory.IDLE),
        ],
    )
    assert ledger.unavailable_time == 1
    assert ledger.idle_time == 1
    assert ledger.available_time == 1


def test_epoch_scale_gap_is_not_hidden_by_relative_tolerance() -> None:
    base = 1_000_000_000.0
    ledger = ActorLedger(
        actor_id="alice",
        intervals=[
            ActorLedgerInterval(start=base, end=base + 1, category=ActorCategory.EXECUTION),
            ActorLedgerInterval(start=base + 1.5, end=base + 2.5, category=ActorCategory.EXECUTION),
        ],
    )
    assert ledger.available_time == pytest.approx(2.0)
    with pytest.raises(ValueError, match="conservation"):
        ledger.assert_complete(start=base, end=base + 2.5)


def test_serial_actor_ledger_rejects_every_positive_overlap() -> None:
    base = 1_000_000_000.0
    with pytest.raises(ValueError, match="overlapping actor capacity"):
        ActorLedger(
            actor_id="alice",
            intervals=[
                ActorLedgerInterval(start=base, end=base + 1, category=ActorCategory.EXECUTION),
                ActorLedgerInterval(
                    start=base + 0.999999,
                    end=base + 2,
                    category=ActorCategory.COORDINATION,
                ),
            ],
        )


def test_fractional_actor_ledger_accepts_allocations_up_to_capacity() -> None:
    ledger = ActorLedger(
        actor_id="alice",
        fractional_multitasking=True,
        intervals=[
            ActorLedgerInterval(
                start=0,
                end=1,
                category=ActorCategory.EXECUTION,
                allocation=0.6,
                work_item_id="a",
            ),
            ActorLedgerInterval(
                start=0,
                end=1,
                category=ActorCategory.EXECUTION,
                allocation=0.4,
                work_item_id="b",
            ),
        ],
    )
    assert ledger.allocated_actor_time == pytest.approx(1.0)

    with pytest.raises(ValueError, match="exceeds actor capacity"):
        ActorLedger(
            actor_id="alice",
            fractional_multitasking=True,
            intervals=[
                ActorLedgerInterval(start=0, end=1, category=ActorCategory.EXECUTION, allocation=0.7),
                ActorLedgerInterval(start=0, end=1, category=ActorCategory.EXECUTION, allocation=0.4),
            ],
        )
