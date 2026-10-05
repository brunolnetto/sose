from __future__ import annotations

import pytest
from pydantic import ValidationError

from sose.organizational.ledger import ActorCategory, ItemCategory
from sose.organizational.projection import AccountingEvent, LedgerProjector


def test_accounting_event_rejects_mismatched_tagged_category() -> None:
    with pytest.raises(ValidationError):
        AccountingEvent(
            event_id="bad-item",
            kind="item",
            target_id="item-1",
            start=0,
            end=1,
        )
    with pytest.raises(ValidationError):
        AccountingEvent(
            event_id="bad-actor",
            kind="actor",
            target_id="alice",
            start=0,
            end=1,
            item_category=ItemCategory.PROCESSING,
        )


def test_projector_segments_overlapping_item_events_and_applies_precedence() -> None:
    projector = LedgerProjector()
    projector.apply(
        AccountingEvent.item(
            event_id="queue",
            work_item_id="item-1",
            start=0,
            end=2,
            category=ItemCategory.QUEUE,
        )
    )
    projector.apply(
        AccountingEvent.item(
            event_id="processing",
            work_item_id="item-1",
            start=1,
            end=2,
            category=ItemCategory.PROCESSING,
        )
    )
    intervals = projector.item_ledger("item-1").intervals
    assert [(interval.start, interval.end, interval.primary) for interval in intervals] == [
        (0, 1, ItemCategory.QUEUE),
        (1, 2, ItemCategory.PROCESSING),
    ]


def test_projector_resolves_rework_over_processing() -> None:
    projector = LedgerProjector()
    for event in (
        AccountingEvent.item(
            event_id="processing",
            work_item_id="item-1",
            start=0,
            end=1,
            category=ItemCategory.PROCESSING,
        ),
        AccountingEvent.item(
            event_id="rework",
            work_item_id="item-1",
            start=0,
            end=1,
            category=ItemCategory.REWORK,
        ),
    ):
        projector.apply(event)
    intervals = projector.item_ledger("item-1").intervals
    assert len(intervals) == 1
    assert intervals[0].primary is ItemCategory.REWORK


def test_fractional_actor_projection_order_is_restart_stable() -> None:
    events = [
        AccountingEvent.actor(
            event_id="b",
            actor_id="alice",
            start=0,
            end=1,
            category=ActorCategory.EXECUTION,
            allocation=0.4,
            work_item_id="b-item",
        ),
        AccountingEvent.actor(
            event_id="a",
            actor_id="alice",
            start=0,
            end=1,
            category=ActorCategory.EXECUTION,
            allocation=0.6,
            work_item_id="a-item",
        ),
    ]
    projector = LedgerProjector()
    for event in events:
        projector.apply(event)

    before = projector.actor_ledger("alice", fractional_multitasking=True)
    rebuilt = LedgerProjector.from_state(projector.dump_state())
    after = rebuilt.actor_ledger("alice", fractional_multitasking=True)

    assert [interval.work_item_id for interval in before.intervals] == ["a-item", "b-item"]
    assert before == after
