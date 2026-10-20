"""Deterministic OD-3 queue semantics independent of unimplemented A1 enums."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sose.organizational.od3_decisions import (
    QueuedWork, OD3Policy, decide_od3, persist_od3_decision,
)
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
OWNER = "org:od3"


def item(name, elapsed_us, service_us, *, blocked=False):
    return QueuedWork(name, OWNER, NOW - timedelta(microseconds=elapsed_us),
                      service_us=service_us, blocked=blocked)


def policy(name, **updates):
    values = dict(name=name, delay_threshold_us=5, cooldown_us=10,
                  decision_cost_us=3, starvation_bound_us=20)
    return OD3Policy(**{**values, **updates})


def decide(queue, p, *, previous=None, observation="observation:1"):
    return decide_od3(owner_id=OWNER, queue=queue, policy=p, observed_at=NOW,
                      previous_decision_at=previous, observation_id=observation)


def test_adaptive_follow_up_contacts_oldest_overdue_without_fabricating_service():
    work = (item("young", 1, 3), item("old", 12, 8))
    decision = decide(work, policy("adaptive_follow_up"))
    assert decision.action == "CONTACT"
    assert decision.selected_item_id == "old"
    assert decision.before == decision.after == ("young", "old")
    assert decision.cost_us == 3
    again = decide(work, policy("adaptive_follow_up"))
    assert again == decision
    cool = decide(work, policy("adaptive_follow_up"),
                  previous=NOW - timedelta(microseconds=2), observation="other")
    assert cool.action == "NOOP" and cool.cost_us == 0


def test_cherry_picking_prefers_short_jobs_before_starvation_bound_then_oldest():
    queue = (item("old", 13, 100), item("fast", 8, 1), item("blocked", 40, 1, blocked=True))
    d = decide(queue, policy("cherry_picking"))
    assert d.action == "PRIORITIZE" and d.selected_item_id == "fast"
    assert d.after == ("fast", "old", "blocked")
    bounded = decide(queue, policy("cherry_picking", starvation_bound_us=12))
    assert bounded.selected_item_id == "old"
    assert "starvation" in bounded.reason
    assert bounded.after == ("old", "fast", "blocked")


def test_charged_sham_is_observational_and_cannot_reorder_work():
    queue = (item("old", 12, 80), item("fast", 7, 1))
    active = decide(queue, policy("cherry_picking"))
    sham = decide(queue, policy("sham"))
    assert sham.action == "SHAM"
    assert sham.before == sham.after
    assert sham.cost_us == active.cost_us == 3
    assert sham.selected_item_id == "old"


@pytest.mark.parametrize("bad", ("foreign", "future", "duplicate", "invalid_cost", "unsupported"))
def test_invalid_observations_and_fake_policy_names_fail_closed(bad):
    work = (item("first", 13, 3), item("second", 7, 5))
    if bad == "foreign":
        work = (replace(work[0], owner_id="foreign"), work[1])
    if bad == "future":
        work = (replace(work[0], arrived_at=NOW + timedelta(days=1)), work[1])
    if bad == "duplicate":
        work = (work[0], replace(work[1], item_id=work[0].item_id))
    if bad == "invalid_cost":
        with pytest.raises(ValueError):
            policy("sham", decision_cost_us=-1)
        return
    if bad == "unsupported":
        with pytest.raises(ValueError, match="unsupported"):
            policy("priority_inflation")
        return
    with pytest.raises((ValueError, PermissionError)):
        decide(work, policy("cherry_picking"))


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_decision_ledger_replay_is_atomic_and_restartable(adapter, tmp_path):
    path = tmp_path / "od3-decisions.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    d = decide((item("work", 20, 10),), policy("adaptive_follow_up"))
    try:
        event = persist_od3_decision(store, d)
        assert persist_od3_decision(store, d) == event
        assert len(store.events()) == 1
        with pytest.raises(ValueError, match="replay conflict"):
            persist_od3_decision(store, replace(d, reason="altered evidence"))
        assert store.events() == (event,)
        assert event.payload["cost_us"] == 3
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as restored:
            assert restored.events() == (event,)
            assert persist_od3_decision(restored, d) == event
