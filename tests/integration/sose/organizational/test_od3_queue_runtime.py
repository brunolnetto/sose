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
        # A later NOOP is still a durable observation. An out-of-order
        # observation must not silently rewrite the queue history.
        second = apply_od3_queue_policy(
            store, owner_id=OWNER, queue_key=KEY, observation_id="later",
            policy=policy("cherry_picking"),
            observed_at=NOW + timedelta(microseconds=2),
        )
        assert second.payload["action"] == "NOOP"
        before = snapshot(store, qid)
        with pytest.raises(ValueError, match="regressed"):
            apply_od3_queue_policy(
                store, owner_id=OWNER, queue_key=KEY, observation_id="past",
                policy=policy("cherry_picking"),
                observed_at=NOW + timedelta(microseconds=1),
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


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_distinct_identical_queues_and_diagnostic_observations_cannot_collide(
    adapter, tmp_path,
):
    from sose.organizational.od3_decisions import (
        decide_od3, persist_od3_decision,
    )

    store = store_for(adapter, tmp_path)
    queue = (work("same", 15, 1),)
    p = policy("adaptive_follow_up")
    try:
        # This preexisting pure diagnostic has the external observation ID.
        diagnostic = decide_od3(
            owner_id=OWNER, queue=queue, policy=p, observed_at=NOW,
            observation_id="same:observation",
        )
        persist_od3_decision(store, diagnostic)
        first_id = seed_od3_queue(
            store, owner_id=OWNER, queue_key="q:first", items=queue, at=NOW,
        )
        second_id = seed_od3_queue(
            store, owner_id=OWNER, queue_key="q:second", items=queue, at=NOW,
        )
        first = apply_od3_queue_policy(
            store, owner_id=OWNER, queue_key="q:first",
            observation_id="same:observation", policy=p, observed_at=NOW,
        )
        second = apply_od3_queue_policy(
            store, owner_id=OWNER, queue_key="q:second",
            observation_id="same:observation", policy=p, observed_at=NOW,
        )
        assert first.event_id != second.event_id
        assert first.causation_id != second.causation_id
        assert first.causation_id != diagnostic.decision_id
        assert second.causation_id != diagnostic.decision_id
        assert snapshot(store, first_id)[0]["paid_policy_us"] == 3
        assert snapshot(store, second_id)[0]["paid_policy_us"] == 3
        assert len(store.events()) == 5  # pure diagnostic + 2 x (decision/effect)
    finally:
        if adapter == "sqlite":
            store.close()


def test_memory_nested_transaction_rejected_before_any_queue_mutation(tmp_path):
    store = MemoryPersistence()
    queue = (work("slow", 15, 30), work("fast", 8, 1))
    qid = seed_od3_queue(
        store, owner_id=OWNER, queue_key=KEY, items=queue, at=NOW,
    )
    before = snapshot(store, qid)
    assert store.transaction_depth == 0
    with store.transaction(owner_id=OWNER) as outer:
        assert store.transaction_depth == 1
        with pytest.raises(RuntimeError, match="nested transaction"):
            apply_od3_queue_policy(
                store, owner_id=OWNER, queue_key=KEY,
                observation_id="no-nesting", policy=policy("cherry_picking"),
                observed_at=NOW,
            )
        with pytest.raises(RuntimeError, match="nested transaction"):
            seed_od3_queue(
                store, owner_id=OWNER, queue_key="nested",
                items=queue, at=NOW,
            )
        assert outer.get_entity(QUEUE_KIND, qid).attributes["paid_policy_us"] == 0
    assert store.transaction_depth == 0
    assert snapshot(store, qid) == before
    with pytest.raises(OSError, match="outer rollback"):
        with store.transaction(owner_id=OWNER):
            assert store.transaction_depth == 1
            raise OSError("outer rollback")
    assert store.transaction_depth == 0
    effect = apply_od3_queue_policy(
        store, owner_id=OWNER, queue_key=KEY, observation_id="no-nesting",
        policy=policy("cherry_picking"), observed_at=NOW,
    )
    assert effect.payload["paid_policy_us"] == 3


@pytest.mark.parametrize("adapter", ("memory", "sqlite", "jsonl", "duckdb"))
def test_every_memory_derived_adapter_rejects_nested_queue_mutation(
    adapter, tmp_path,
):
    from sose.core.events import DomainEvent
    from sose.persistence.jsonl_journal import JSONLJournalPersistence
    from sose.persistence.duckdb import DuckDBPersistence

    constructors = {
        "memory": lambda: MemoryPersistence(),
        "sqlite": lambda: SQLitePersistence(tmp_path / "nested.sqlite"),
        "jsonl": lambda: JSONLJournalPersistence(tmp_path / "nested.jsonl"),
        "duckdb": lambda: DuckDBPersistence(tmp_path / "nested.duckdb"),
    }
    store = constructors[adapter]()
    queue = (work("old", 15, 100), work("short", 8, 1))
    qid = seed_od3_queue(
        store, owner_id=OWNER, queue_key=KEY, items=queue, at=NOW,
    )
    marker = DomainEvent(
        event_id="outer:event", name="outer.preserved",
        entity_type="test", entity_id="marker",
        occurred_at=NOW, owner_id=OWNER,
    )
    try:
        with store.transaction(owner_id=OWNER) as outer:
            outer.append_event(marker)
            assert store.transaction_depth == 1
            with pytest.raises(RuntimeError, match="nested transaction"):
                apply_od3_queue_policy(
                    store, owner_id=OWNER, queue_key=KEY,
                    observation_id="nested:policy",
                    policy=policy("cherry_picking"), observed_at=NOW,
                )
            with pytest.raises(RuntimeError, match="nested transaction"):
                seed_od3_queue(
                    store, owner_id=OWNER, queue_key="nested:new",
                    items=queue, at=NOW,
                )
        assert store.transaction_depth == 0
        assert snapshot(store, qid)[0]["paid_policy_us"] == 0
        assert tuple(event.name for event in store.events()) == ("outer.preserved",)
        effect = apply_od3_queue_policy(
            store, owner_id=OWNER, queue_key=KEY,
            observation_id="nested:policy",
            policy=policy("cherry_picking"), observed_at=NOW,
        )
        assert effect.payload["paid_policy_us"] == 3
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    if adapter == "jsonl":
        recovered = JSONLJournalPersistence(tmp_path / "nested.jsonl")
        assert snapshot(recovered, qid)[0]["paid_policy_us"] == 3
        assert tuple(event.name for event in recovered.events()) == (
            "outer.preserved", "od3.policy_decided", "od3.queue_effect_applied",
        )


@pytest.mark.parametrize("bad_id", ("", " ", "  \t", "  padded", None, 42))
def test_invalid_work_identity_is_rejected_before_any_queue_is_admitted(bad_id):
    with pytest.raises(ValueError, match="canonical nonblank"):
        QueuedWork(
            item_id=bad_id, owner_id=OWNER,
            arrived_at=NOW, service_us=1,
        )


@pytest.mark.parametrize("adapter", ("memory", "jsonl"))
def test_inherited_async_transaction_context_expires_after_parent_exit(adapter, tmp_path):
    import asyncio
    from sose.persistence.jsonl_journal import JSONLJournalPersistence

    store = (MemoryPersistence() if adapter == "memory"
             else JSONLJournalPersistence(tmp_path / "async-context.jsonl"))

    async def exercise():
        async def child_created_under_parent():
            # The task inherited the parent's context, but must not inherit
            # a permanently live transaction after its parent has committed.
            assert store.transaction_depth == 0
            seed_od3_queue(
                store, owner_id=OWNER, queue_key="async:child",
                items=(work("child-item", 8, 1),), at=NOW,
            )
            return store.transaction_depth

        with store.transaction(owner_id=OWNER):
            assert store.transaction_depth == 1
            child = asyncio.create_task(child_created_under_parent())
        assert store.transaction_depth == 0
        return await child

    assert asyncio.run(exercise()) == 0
    child_id = seed_od3_queue(
        store, owner_id=OWNER, queue_key="async:other",
        items=(work("other", 8, 1),), at=NOW,
    )
    assert snapshot(store, child_id)[1] == 0


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("corruption, expected", (
    ("queue_shape", "provenance/shape mismatch"),
    ("owner", "provenance/shape mismatch"),
    ("wrong_schema", "provenance/shape mismatch"),
    ("negative_paid", "provenance/shape mismatch"),
    ("string_last_action", "timestamp malformed"),
    ("malformed_last_observed", "Invalid isoformat string"),
    ("item_shape", "item schema mismatch"),
    ("non_boolean_blocked", "non-boolean blocked"),
    ("malformed_item_timestamp", "item is invalid"),
    ("duplicate_item", "duplicate or foreign"),
    ("foreign_item", "duplicate or foreign"),
    ("future_creation", "precedes queue admission"),
))
def test_durable_queue_rejects_corrupt_provenance_without_new_effects(
    adapter, corruption, expected, tmp_path,
):
    store = store_for(adapter, tmp_path)
    try:
        queue_id = seed_od3_queue(
            store, owner_id=OWNER, queue_key=KEY,
            items=(work("first", 15, 40), work("second", 8, 1)), at=NOW,
        )
        with store.transaction(owner_id=OWNER) as uow:
            entity = uow.get_entity(QUEUE_KIND, queue_id)
            attrs = entity.attributes
            if corruption == "queue_shape":
                attrs["unknown"] = True
            elif corruption == "owner":
                attrs["owner_id"] = "owner:forged"
            elif corruption == "wrong_schema":
                attrs["schema_version"] = 999
            elif corruption == "negative_paid":
                attrs["paid_policy_us"] = -1
            elif corruption == "string_last_action":
                attrs["last_action_at"] = 12
            elif corruption == "malformed_last_observed":
                attrs["last_observed_at"] = "invalid-time"
            elif corruption == "item_shape":
                attrs["items"][0]["unexpected"] = 1
            elif corruption == "non_boolean_blocked":
                attrs["items"][0]["blocked"] = 1
            elif corruption == "malformed_item_timestamp":
                attrs["items"][0]["arrived_at"] = "malformed"
            elif corruption == "duplicate_item":
                attrs["items"][1]["item_id"] = attrs["items"][0]["item_id"]
            elif corruption == "foreign_item":
                attrs["items"][0]["owner_id"] = "owner:wrong"
            elif corruption == "future_creation":
                entity.created_at = NOW + timedelta(seconds=1)
            uow.save_entity(entity)
        before = store.events()
        with pytest.raises(ValueError, match=expected):
            apply_od3_queue_policy(
                store, owner_id=OWNER, queue_key=KEY,
                observation_id="corruption-check",
                policy=policy("cherry_picking"), observed_at=NOW,
            )
        assert store.events() == before
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("corruption", ("missing_effect", "altered_effect"))
def test_queue_replay_rejects_missing_or_altered_durable_effect(
    adapter, corruption, tmp_path,
):
    from dataclasses import replace as dataclass_replace
    store = store_for(adapter, tmp_path)
    try:
        queue_id = seed_od3_queue(
            store, owner_id=OWNER, queue_key=KEY,
            items=(work("long", 15, 100), work("short", 8, 1)), at=NOW,
        )
        effect = apply_od3_queue_policy(
            store, owner_id=OWNER, queue_key=KEY,
            observation_id="real", policy=policy("cherry_picking"), observed_at=NOW,
        )
        with store.transaction(owner_id=OWNER) as uow:
            match = next(e for e in uow.events() if e.event_id == effect.event_id)
            uow._working.events.remove(match)
            if corruption == "altered_effect":
                uow.append_event(dataclass_replace(match, entity_id="not-our-queue"))
        with pytest.raises(ValueError, match="lacks durable effect"):
            apply_od3_queue_policy(
                store, owner_id=OWNER, queue_key=KEY,
                observation_id="real", policy=policy("cherry_picking"), observed_at=NOW,
            )
        assert store.entity(QUEUE_KIND, queue_id).attributes["paid_policy_us"] == 3
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("reservation", ("decision", "effect"))
def test_queue_refuses_preoccupied_deterministic_evidence_identity(
    adapter, reservation, tmp_path,
):
    from sose.core.events import DomainEvent
    from sose.core.identity import deterministic_id
    from sose.organizational.od3_decisions import decide_od3

    store = store_for(adapter, tmp_path)
    initial = (work("long", 15, 100), work("short", 8, 1))
    try:
        queue_id = seed_od3_queue(
            store, owner_id=OWNER, queue_key=KEY, items=initial, at=NOW,
        )
        selected_policy = policy("cherry_picking")
        decision = decide_od3(
            owner_id=OWNER, queue=initial, policy=selected_policy,
            observed_at=NOW,
            observation_id=deterministic_id(
                "od3-operational-observation", queue_id, "claimed",
            ),
        )
        identity = (
            decision.decision_id if reservation == "decision" else
            deterministic_id("od3-queue-effect", queue_id, decision.decision_id)
        )
        with store.transaction(owner_id=OWNER) as uow:
            uow.append_event(DomainEvent(
                event_id=identity, name="adversarial.reserved",
                entity_type="test", entity_id=queue_id,
                owner_id=OWNER, occurred_at=NOW,
            ))
        old = tuple(store.events())
        with pytest.raises(ValueError, match=(
            "decision identity already used" if reservation == "decision"
            else "operational effect identity already exists"
        )):
            apply_od3_queue_policy(
                store, owner_id=OWNER, queue_key=KEY,
                observation_id="claimed", policy=selected_policy, observed_at=NOW,
            )
        assert tuple(store.events()) == old
        assert store.entity(QUEUE_KIND, queue_id).attributes["paid_policy_us"] == 0
    finally:
        if adapter == "sqlite":
            store.close()


def test_policy_rejects_invalid_external_requests_before_transaction():
    store = MemoryPersistence()
    with pytest.raises(ValueError, match="canonical nonblank"):
        seed_od3_queue(store, owner_id=" ", queue_key=KEY, items=(), at=NOW)
    with pytest.raises(ValueError, match="canonical nonblank"):
        apply_od3_queue_policy(
            store, owner_id=OWNER, queue_key=" ", observation_id="id",
            policy=policy("cherry_picking"), observed_at=NOW,
        )
    with pytest.raises(TypeError, match="tuple"):
        seed_od3_queue(store, owner_id=OWNER, queue_key=KEY, items=[], at=NOW)
    with pytest.raises(TypeError, match="typed policy"):
        apply_od3_queue_policy(
            store, owner_id=OWNER, queue_key=KEY, observation_id="id",
            policy=None, observed_at=NOW,
        )
    assert not store.events()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("tamper", (
    "paid_cost", "last_action", "fake_observation", "rank",
))
def test_durable_od3_policy_refuses_forged_cost_or_observation_history(
    adapter, tamper, tmp_path,
):
    """#463: a public projection cannot mint paid policy time or new rank."""
    store = store_for(adapter, tmp_path)
    try:
        queue_id = seed_od3_queue(
            store, owner_id=OWNER, queue_key=KEY,
            items=(work("slow", 15, 70), work("fast", 8, 1)), at=NOW,
        )
        apply_od3_queue_policy(
            store, owner_id=OWNER, queue_key=KEY,
            observation_id="first", policy=policy("cherry_picking"), observed_at=NOW,
        )
        with store.transaction(owner_id=OWNER) as uow:
            queue = uow.get_entity(QUEUE_KIND, queue_id)
            if tamper == "paid_cost":
                queue.attributes["paid_policy_us"] += 5
            elif tamper == "last_action":
                queue.attributes["last_action_at"] = (
                    NOW + timedelta(microseconds=1)
                ).isoformat()
            elif tamper == "fake_observation":
                queue.attributes["applied_observations"]["ghost"] = {
                    "request_id": "forged", "effect_event_id": "forged",
                }
            else:
                queue.attributes["items"].reverse()
            uow.save_entity(queue)
        old = snapshot(store, queue_id)
        with pytest.raises(ValueError, match="OD-3 charged queue"):
            apply_od3_queue_policy(
                store, owner_id=OWNER, queue_key=KEY,
                observation_id="next", policy=policy("cherry_picking"),
                observed_at=NOW + timedelta(microseconds=21),
            )
        assert snapshot(store, queue_id) == old
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_reconciled_od3_policy_cost_accumulates_after_restart(adapter, tmp_path):
    store = store_for(adapter, tmp_path)
    try:
        queue_id = seed_od3_queue(
            store, owner_id=OWNER, queue_key=KEY,
            items=(work("slow", 15, 70), work("fast", 8, 1)), at=NOW,
        )
        p = policy("cherry_picking")
        apply_od3_queue_policy(
            store, owner_id=OWNER, queue_key=KEY,
            observation_id="one", policy=p, observed_at=NOW,
        )
        apply_od3_queue_policy(
            store, owner_id=OWNER, queue_key=KEY,
            observation_id="two", policy=p,
            observed_at=NOW + timedelta(microseconds=21),
        )
        attrs, version, _ = snapshot(store, queue_id)
        assert version == 2
        assert attrs["paid_policy_us"] in (3, 6)
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(tmp_path / "sqlite.sqlite") as reopened:
            assert snapshot(reopened, queue_id)[0] == attrs
