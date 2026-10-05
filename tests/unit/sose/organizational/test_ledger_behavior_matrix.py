from __future__ import annotations

import pytest

from sose.organizational.ledger import (
    ActorCategory,
    ActorLedger,
    ActorLedgerInterval,
    ItemCategory,
    ItemLedger,
    ItemLedgerInterval,
)
from sose.organizational.model_spec import InterventionClass, ModelIntervention, ModelSpec
from sose.organizational.projection import (
    AccountingEvent,
    LedgerProjector,
    resolve_item_primary,
    split_item_interval,
)


def _empty_spec() -> ModelSpec:
    return ModelSpec(parameters={}, parameter_evidence={})


def test_simple_processing() -> None:
    item = ItemLedger(
        work_item_id="x",
        intervals=[ItemLedgerInterval(start=9, end=10, primary=ItemCategory.PROCESSING)],
    )
    actor = ActorLedger(
        actor_id="alice",
        intervals=[ActorLedgerInterval(start=9, end=10, category=ActorCategory.EXECUTION)],
    )
    assert item.elapsed == 1
    assert actor.allocated_actor_time == 1


def test_queue_wait_plus_processing_conserves_elapsed_time() -> None:
    ledger = ItemLedger(
        work_item_id="x",
        intervals=[
            ItemLedgerInterval(start=9, end=10, primary=ItemCategory.QUEUE),
            ItemLedgerInterval(start=10, end=11, primary=ItemCategory.PROCESSING),
        ],
    )
    ledger.assert_complete(created_at=9, observed_at=11)
    assert ledger.elapsed == 2


def test_calendar_and_decision_wait_do_not_double_count() -> None:
    ledger = ItemLedger(
        work_item_id="x",
        intervals=[
            ItemLedgerInterval(start=0, end=64, primary=ItemCategory.WAITING_CALENDAR),
            ItemLedgerInterval(start=64, end=65, primary=ItemCategory.WAITING_DECISION),
            ItemLedgerInterval(start=65, end=65.5, primary=ItemCategory.COORDINATION),
        ],
    )
    ledger.assert_complete(created_at=0, observed_at=65.5)
    assert sum(ledger.duration_by_primary().values()) == pytest.approx(65.5)


def test_meeting_consumes_multiple_actor_hours_but_one_item_hour() -> None:
    item = ItemLedger(
        work_item_id="x",
        intervals=[ItemLedgerInterval(start=0, end=1, primary=ItemCategory.COORDINATION)],
    )
    actors = [
        ActorLedger(
            actor_id=name,
            intervals=[ActorLedgerInterval(start=0, end=1, category=ActorCategory.COORDINATION)],
        )
        for name in ("a", "b", "c")
    ]
    assert item.elapsed == 1
    assert sum(actor.allocated_actor_time for actor in actors) == 3


def test_shared_meeting_can_advance_five_items_without_creating_actor_time() -> None:
    items = [
        ItemLedger(
            work_item_id=str(index),
            intervals=[ItemLedgerInterval(start=0, end=1, primary=ItemCategory.COORDINATION)],
        )
        for index in range(5)
    ]
    actors = [
        ActorLedger(
            actor_id=name,
            intervals=[ActorLedgerInterval(start=0, end=1, category=ActorCategory.COORDINATION)],
        )
        for name in ("a", "b", "c")
    ]
    assert sum(item.elapsed for item in items) == 5
    assert sum(actor.allocated_actor_time for actor in actors) == 3


def test_preemption_and_context_reacquisition_conserve_actor_time() -> None:
    ledger = ActorLedger(
        actor_id="alice",
        intervals=[
            ActorLedgerInterval(start=0, end=1, category=ActorCategory.EXECUTION, work_item_id="a"),
            ActorLedgerInterval(start=1, end=1.5, category=ActorCategory.EXECUTION, work_item_id="incident"),
            ActorLedgerInterval(
                start=1.5,
                end=1.75,
                category=ActorCategory.EXECUTION,
                work_item_id="a",
                secondary_labels=("context_reacquisition",),
            ),
            ActorLedgerInterval(start=1.75, end=2.5, category=ActorCategory.EXECUTION, work_item_id="a"),
        ],
    )
    ledger.assert_complete(start=0, end=2.5)
    assert ledger.allocated_actor_time == 2.5


def test_idle_and_unavailable_are_distinct() -> None:
    ledger = ActorLedger(
        actor_id="alice",
        intervals=[
            ActorLedgerInterval(start=9, end=12, category=ActorCategory.IDLE),
            ActorLedgerInterval(start=12, end=13, category=ActorCategory.UNAVAILABLE),
            ActorLedgerInterval(start=13, end=17, category=ActorCategory.EXECUTION),
        ],
    )
    assert ledger.idle_time == 3
    assert ledger.unavailable_time == 1
    assert ledger.utilization == pytest.approx(4 / 7)


def test_serial_multitasking_is_rejected() -> None:
    with pytest.raises(ValueError, match="overlapping actor capacity"):
        ActorLedger(
            actor_id="alice",
            intervals=[
                ActorLedgerInterval(start=0, end=1, category=ActorCategory.EXECUTION),
                ActorLedgerInterval(start=0, end=1, category=ActorCategory.COORDINATION),
            ],
        )


def test_fractional_multitasking_conserves_capacity() -> None:
    ledger = ActorLedger(
        actor_id="alice",
        fractional_multitasking=True,
        intervals=[
            ActorLedgerInterval(start=0, end=1, category=ActorCategory.EXECUTION, allocation=0.6),
            ActorLedgerInterval(start=0, end=1, category=ActorCategory.EXECUTION, allocation=0.4),
        ],
    )
    assert ledger.allocated_actor_time == 1


def test_a1_adaptation_is_accounted_separately_from_execution() -> None:
    ledger = ActorLedger(
        actor_id="alice",
        intervals=[
            ActorLedgerInterval(start=0, end=10, category=ActorCategory.ADAPTATION),
            ActorLedgerInterval(start=10, end=60, category=ActorCategory.EXECUTION),
        ],
    )
    assert ledger.duration_by_category()[ActorCategory.ADAPTATION] == 10
    assert ledger.duration_by_category()[ActorCategory.EXECUTION] == 50


def test_a2_governance_coincides_with_explicit_configuration_change() -> None:
    before = _empty_spec()
    intervention = ModelIntervention(
        intervention_id="wip-change",
        intervention_class=InterventionClass.ADAPTIVE_MANAGERIAL,
        set_values={"/policies/wip_limit": 3},
    )
    after, _ = intervention.apply(before)
    manager = ActorLedger(
        actor_id="manager",
        intervals=[ActorLedgerInterval(start=0, end=30, category=ActorCategory.GOVERNANCE)],
    )
    assert manager.allocated_actor_time == 30
    assert before.model_spec_hash != after.model_spec_hash


def test_restart_equivalence_for_projected_ledgers() -> None:
    events = [
        AccountingEvent.item(
            event_id="e1", work_item_id="x", start=0, end=1, category=ItemCategory.QUEUE
        ),
        AccountingEvent.item(
            event_id="e2", work_item_id="x", start=1, end=2, category=ItemCategory.PROCESSING
        ),
    ]
    continuous = LedgerProjector()
    for event in events:
        continuous.apply(event)

    rebuilt = LedgerProjector()
    rebuilt.apply(events[0])
    rebuilt = LedgerProjector.from_state(rebuilt.dump_state())
    rebuilt.apply(events[1])
    assert continuous.normalized() == rebuilt.normalized()


def test_replay_is_idempotent() -> None:
    event = AccountingEvent.item(
        event_id="e1", work_item_id="x", start=0, end=1, category=ItemCategory.PROCESSING
    )
    projector = LedgerProjector()
    projector.apply(event)
    projector.apply(event)
    assert projector.item_ledger("x").elapsed == 1


def test_structural_change_splits_interval_at_configuration_boundary() -> None:
    interval = ItemLedgerInterval(
        start=0,
        end=10,
        primary=ItemCategory.PROCESSING,
        model_spec_hash="before",
    )
    left, right = split_item_interval(interval, at=4, next_model_spec_hash="after")
    assert (left.start, left.end, left.model_spec_hash) == (0, 4, "before")
    assert (right.start, right.end, right.model_spec_hash) == (4, 10, "after")


def test_rework_precedes_processing() -> None:
    assert resolve_item_primary(ItemCategory.PROCESSING, ItemCategory.REWORK) is ItemCategory.REWORK


def test_calendar_wait_precedes_decision_wait() -> None:
    assert (
        resolve_item_primary(ItemCategory.WAITING_DECISION, ItemCategory.WAITING_CALENDAR)
        is ItemCategory.WAITING_CALENDAR
    )


@pytest.mark.parametrize("duration", [0.25, 1.0, 7.5, 100.0])
def test_item_conservation_property_over_positive_durations(duration: float) -> None:
    ledger = ItemLedger(
        work_item_id="x",
        intervals=[ItemLedgerInterval(start=0, end=duration, primary=ItemCategory.PROCESSING)],
    )
    ledger.assert_complete(created_at=0, observed_at=duration)
    assert ledger.elapsed == pytest.approx(duration)
