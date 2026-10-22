"""Operational OD-3 queue effects, exact replay and paid-decision accounting (#463)."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sose.organizational.od3_decisions import OD3Policy, QueuedWork
from sose.organizational.od3_queue_runtime import (
    QUEUE_KIND, apply_od3_queue_policy, seed_od3_queue,
)
from sose.persistence.memory import MemoryPersistence, MemoryUnitOfWork
from sose.persistence.sqlite import SQLitePersistence

NOW = datetime(2026, 10, 22, 10, tzinfo=timezone.utc)
OWNER = "org:od3"
KEY = "workstream:orders"


def policy(name, **overrides):
    return OD3Policy(**{
        **dict(name=name, delay_threshold_us=5, cooldown_us=10,
               decision_cost_us=3, starvation_bound_us=20),
        **overrides,
    })


def work(name, wait_us, effort, *, last_contact_us=None, blocked=False):
    return QueuedWork(
        item_id=name, owner_id=OWNER,
        arrived_at=NOW - timedelta(microseconds=wait_us),
        service_us=effort,
        last_contact_at=(
            NOW - timedelta(microseconds=last_contact_us)
            if last_contact_us is not None else None
        ),
        blocked=blocked,
    )


def store_for(adapter, tmp_path):
    return (MemoryPersistence() if adapter == "memory" else
            SQLitePersistence(tmp_path / (adapter + ".sqlite")))


def snapshot(store, queue_id):
    row = store.entity(QUEUE_KIND, queue_id)
    assert row is not None
    return row.attributes, row.version, store.events()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_cherry_picking_changes_durable_rank_while_paid_sham_does_not(adapter, tmp_path):
    store = store_for(adapter, tmp_path)
    queue = (work("slow", 15, 70), work("fast", 8, 1))
    p = policy("cherry_picking")
    sham = policy("sham", matched_policy="cherry_picking")
    try:
        active_id = seed_od3_queue(
            store, owner_id=OWNER, queue_key="active", items=queue, at=NOW,
        )
        sham_id = seed_od3_queue(
            store, owner_id=OWNER, queue_key="sham", items=queue, at=NOW,
        )
        active = apply_od3_queue_policy(
            store, owner_id=OWNER, queue_key="active", observation_id="paired:1",
            policy=p, observed_at=NOW,
        )
        placebo = apply_od3_queue_policy(
            store, owner_id=OWNER, queue_key="sham", observation_id="paired:1",
            policy=sham, matched_active_policy=p, observed_at=NOW,
        )
        attrs_active, version_active, _ = snapshot(store, active_id)
        attrs_sham, version_sham, _ = snapshot(store, sham_id)
        assert active.name == placebo.name == "od3.queue_effect_applied"
        assert active.payload["action"] == "PRIORITIZE"
        assert placebo.payload["action"] == "SHAM"
        assert active.payload["selected_item_id"] == placebo.payload["selected_item_id"] == "fast"
        assert [x["item_id"] for x in attrs_active["items"]] == ["fast", "slow"]
        assert [x["item_id"] for x in attrs_sham["items"]] == ["slow", "fast"]
        assert attrs_active["paid_policy_us"] == attrs_sham["paid_policy_us"] == 3
        assert version_active == version_sham == 1
        assert len(store.events()) == 4
        assert {ev.name for ev in store.events()} == {
            "od3.policy_decided", "od3.queue_effect_applied",
        }
        # A replay is harmless even after the queue was already modified.
        assert apply_od3_queue_policy(
            store, owner_id=OWNER, queue_key="active", observation_id="paired:1",
            policy=p, observed_at=NOW,
        ) == active
        with pytest.raises(ValueError, match="replay conflicts"):
            apply_od3_queue_policy(
                store, owner_id=OWNER, queue_key="active", observation_id="paired:1",
                policy=replace(p, decision_cost_us=8), observed_at=NOW,
            )
        assert snapshot(store, active_id)[1] == 1
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(tmp_path / "sqlite.sqlite") as recovered:
            assert apply_od3_queue_policy(
                recovered, owner_id=OWNER, queue_key="active",
                observation_id="paired:1", policy=p, observed_at=NOW,
            ) == active
            assert snapshot(recovered, active_id)[0]["paid_policy_us"] == 3


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_follow_up_updates_contact_and_cooldown_without_inventing_service(adapter, tmp_path):
    store = store_for(adapter, tmp_path)
    p = policy("adaptive_follow_up")
    queue = (work("old", 15, 80), work("later", 10, 1))
    try:
        queue_id = seed_od3_queue(store, owner_id=OWNER, queue_key=KEY, items=queue, at=NOW)
        first = apply_od3_queue_policy(
            store, owner_id=OWNER, queue_key=KEY,
            observation_id="contact:1", policy=p, observed_at=NOW,
        )
        state, _, _ = snapshot(store, queue_id)
        assert first.payload["action"] == "CONTACT"
        assert first.payload["selected_item_id"] == "old"
        assert state["items"][0]["last_contact_at"] == NOW.isoformat()
        assert [x["item_id"] for x in state["items"]] == ["old", "later"]
        assert state["paid_policy_us"] == 3
        noop = apply_od3_queue_policy(
            store, owner_id=OWNER, queue_key=KEY,
            observation_id="contact:2", policy=p,
            observed_at=NOW + timedelta(microseconds=2),
        )
        assert noop.payload["action"] == "NOOP" and noop.payload["cost_us"] == 0
        # Last action remains the paid contact, not the null observation.
        assert snapshot(store, queue_id)[0]["last_action_at"] == NOW.isoformat()
        next_action = apply_od3_queue_policy(
            store, owner_id=OWNER, queue_key=KEY,
            observation_id="contact:3", policy=p,
            observed_at=NOW + timedelta(microseconds=12),
        )
        assert next_action.payload["action"] == "CONTACT"
        assert next_action.payload["selected_item_id"] == "later"
        assert snapshot(store, queue_id)[0]["paid_policy_us"] == 6
        assert len(store.events()) == 6
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_starvation_guard_and_replay_conflict_fail_closed(adapter, tmp_path):
    store = store_for(adapter, tmp_path)
    try:
        qid = seed_od3_queue(
            store, owner_id=OWNER, queue_key=KEY,
            items=(work("old", 21, 80), work("fast", 8, 1)), at=NOW,
        )
        first = apply_od3_queue_policy(
            store, owner_id=OWNER, queue_key=KEY, observation_id="starvation",
            policy=policy("cherry_picking"), observed_at=NOW,
        )
        assert first.payload["selected_item_id"] == "old"
        assert first.payload["after"] == ["old", "fast"]
        before = snapshot(store, qid)
        with pytest.raises(ValueError, match="replay conflicts"):
            apply_od3_queue_policy(
                store, owner_id=OWNER, queue_key=KEY, observation_id="starvation",
                policy=policy("adaptive_follow_up"), observed_at=NOW,
            )
        with pytest.raises(ValueError, match="regressed"):
            apply_od3_queue_policy(
                store, owner_id=OWNER, queue_key=KEY, observation_id="past",
                policy=policy("cherry_picking"),
                observed_at=NOW - timedelta(microseconds=1),
            )
        with pytest.raises(KeyError, match="not admitted"):
            apply_od3_queue_policy(
                store, owner_id=OWNER, queue_key="missing", observation_id="missing",
                policy=policy("cherry_picking"), observed_at=NOW,
            )
        assert snapshot(store, qid) == before
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_queue_effect_and_paid_ledger_roll_back_on_late_append_failure(
    adapter, tmp_path, monkeypatch,
):
    store = store_for(adapter, tmp_path)
    qid = seed_od3_queue(
        store, owner_id=OWNER, queue_key=KEY,
        items=(work("slow", 15, 30), work("fast", 8, 1)), at=NOW,
    )
    original_append = MemoryUnitOfWork.append_event

    def fail_effect(self, event):
        if event.name == "od3.queue_effect_applied":
            raise OSError("injected failure after diagnostic append")
        return original_append(self, event)

    before = snapshot(store, qid)
    try:
        with monkeypatch.context() as mp:
            mp.setattr(MemoryUnitOfWork, "append_event", fail_effect)
            with pytest.raises(OSError, match="injected failure"):
                apply_od3_queue_policy(
                    store, owner_id=OWNER, queue_key=KEY,
                    observation_id="atomic", policy=policy("cherry_picking"),
                    observed_at=NOW,
                )
        assert snapshot(store, qid) == before
        effect = apply_od3_queue_policy(
            store, owner_id=OWNER, queue_key=KEY, observation_id="atomic",
            policy=policy("cherry_picking"), observed_at=NOW,
        )
        assert effect.name == "od3.queue_effect_applied"
        assert snapshot(store, qid)[0]["paid_policy_us"] == 3
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(tmp_path / "sqlite.sqlite") as recovered:
            assert snapshot(recovered, qid)[0]["paid_policy_us"] == 3


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_reject_queue_admission_conflicts_and_foreign_work(adapter, tmp_path):
    store = store_for(adapter, tmp_path)
    try:
        qid = seed_od3_queue(
            store, owner_id=OWNER, queue_key=KEY, items=(work("good", 8, 2),), at=NOW,
        )
        with pytest.raises(ValueError, match="already been admitted"):
            seed_od3_queue(
                store, owner_id=OWNER, queue_key=KEY,
                items=(work("other", 8, 2),), at=NOW,
            )
        with pytest.raises(ValueError, match="foreign or future"):
            seed_od3_queue(
                store, owner_id=OWNER, queue_key="other",
                items=(replace(work("good", 8, 2), owner_id="foreign"),), at=NOW,
            )
        with pytest.raises(ValueError, match="foreign or future"):
            seed_od3_queue(
                store, owner_id=OWNER, queue_key="future",
                items=(work("future", -1, 2),), at=NOW,
            )
        with pytest.raises(ValueError, match="foreign or future"):
            seed_od3_queue(
                store, owner_id=OWNER, queue_key="duplicate",
                items=(work("same", 8, 2), work("same", 7, 3)), at=NOW,
            )
        with pytest.raises(KeyError, match="not admitted"):
            apply_od3_queue_policy(
                store, owner_id="foreign", queue_key=KEY, observation_id="foreign",
                policy=policy("cherry_picking"), observed_at=NOW,
            )
        assert snapshot(store, qid)[1] == 0
    finally:
        if adapter == "sqlite":
            store.close()
