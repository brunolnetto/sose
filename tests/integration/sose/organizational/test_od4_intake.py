"""OD-4 bounded intervention TDD: genuine intake, paid sham, rollback/restart."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from sose.core.identity import deterministic_id
from sose.organizational.od4_intake import (
    WORK_KIND, IntakeProtocol, admit_work, intervene_work,
)
from sose.persistence.memory import MemoryPersistence, MemoryUnitOfWork
from sose.persistence.sqlite import SQLitePersistence

T0 = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
OWNER = "org:management-a"
OTHER = "org:management-b"
PROTOCOL = IntakeProtocol(
    model_spec_hash="od4-reference-model-v1",
    config_epoch=4, cost_us=11,
    protocol_id="od4-admission-observation-v1",
)


def _store(adapter, tmp_path):
    return MemoryPersistence() if adapter == "memory" else SQLitePersistence(
        tmp_path / "management.sqlite",
    )


def _run(store, key, *, cause="observation:1", action="accept",
         protocol=PROTOCOL, at=T0 + timedelta(minutes=1),
         owner=OWNER, matched=None):
    return intervene_work(
        store, owner_id=owner, work_key=key, cause_id=cause,
        action=action, protocol=protocol, at=at, matched_action=matched,
    )


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("action", ("accept", "reject"))
def test_native_intake_change_and_charged_sham_have_same_cost_not_same_effect(
    adapter, action, tmp_path,
):
    expected_state = {"accept": "accepted", "reject": "rejected"}[action]
    store = _store(adapter, tmp_path)
    try:
        active_id = admit_work(
            store, owner_id=OWNER, work_key="active",
            protocol=PROTOCOL, at=T0,
        )
        sham_id = admit_work(
            store, owner_id=OWNER, work_key="sham",
            protocol=PROTOCOL, at=T0,
        )
        active = _run(store, "active", action=action)
        control = _run(store, "sham", action="sham", matched=action)
        assert active.name == control.name == "od4.intake_decided"
        assert active.payload["before"] == control.payload["before"] == "pending"
        assert active.payload["after"] == expected_state
        assert control.payload["after"] == "pending"
        assert active.payload["source_event_id"] is not None
        assert control.payload["source_event_id"] is None
        assert active.payload["cost_us"] == control.payload["cost_us"] == 11
        active_entity = store.entity(WORK_KIND, active_id)
        sham_entity = store.entity(WORK_KIND, sham_id)
        assert active_entity.state == expected_state and sham_entity.state == "pending"
        assert active_entity.version == sham_entity.version == 1
        assert (active_entity.attributes["paid_management_us"] ==
                sham_entity.attributes["paid_management_us"] == 11)
        sources = tuple(e for e in store.events() if (
            e.name == "entity.state_transition" and e.entity_id == active_id
        ))
        assert len(sources) == 1
        assert sources[0].payload["trigger"] == action
        assert active.payload["source_event_id"] == sources[0].event_id
        assert len(store.events()) == 5   # 2 admission + 1 chart + 2 decisions

        original = tuple(store.events())
        assert _run(store, "active", action=action) == active
        assert _run(store, "sham", action="sham", matched=action) == control
        assert store.events() == original
        with pytest.raises(ValueError, match="terminal"):
            _run(store, "active", cause="attempt:2", action="reject"
                 if action == "accept" else "accept")
        assert store.events() == original
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(tmp_path / "management.sqlite") as recovered:
            assert _run(recovered, "active", action=action) == active
            assert _run(recovered, "sham", action="sham", matched=action) == control
            assert recovered.entity(WORK_KIND, active_id).state == expected_state
            assert recovered.entity(WORK_KIND, sham_id).state == "pending"


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_cooldown_time_epoch_protocol_and_replay_conflict_fail_closed(adapter, tmp_path):
    store = _store(adapter, tmp_path)
    try:
        work_id = admit_work(
            store, owner_id=OWNER, work_key="case", protocol=PROTOCOL, at=T0,
        )
        baseline = tuple(store.events())
        with pytest.raises(ValueError, match="predates admission"):
            _run(store, "case", at=T0 - timedelta(seconds=1))
        with pytest.raises(ValueError, match="admission/protocol"):
            _run(store, "case", protocol=replace(PROTOCOL, config_epoch=5))
        with pytest.raises(ValueError, match="admission/protocol"):
            _run(store, "case", protocol=replace(PROTOCOL, cost_us=13))
        with pytest.raises(ValueError, match="sham requires"):
            _run(store, "case", action="sham")
        with pytest.raises(ValueError, match="sham requires"):
            _run(store, "case", action="accept", matched="reject")
        with pytest.raises(ValueError, match="unsupported"):
            _run(store, "case", action="reassign")
        with pytest.raises(KeyError, match="not admitted"):
            _run(store, "missing")
        with pytest.raises(KeyError, match="not admitted"):
            _run(store, "case", owner=OTHER)
        assert tuple(store.events()) == baseline
        first = _run(store, "case", action="sham", matched="accept")
        before = tuple(store.events())
        with pytest.raises(ValueError, match="cause conflicts"):
            _run(store, "case", action="sham", matched="reject")
        with pytest.raises(ValueError, match="cause conflicts"):
            _run(store, "case", action="sham", matched="accept",
                 at=T0 + timedelta(minutes=2))
        with pytest.raises(ValueError, match="time regressed"):
            _run(store, "case", cause="old", action="sham", matched="accept",
                 at=T0)
        assert tuple(store.events()) == before
        assert first.payload["after"] == "pending"
        assert store.entity(WORK_KIND, work_id).attributes["paid_management_us"] == 11
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_admission_is_immutable_even_if_actor_projection_is_tampered(adapter, tmp_path):
    store = _store(adapter, tmp_path)
    try:
        ident = admit_work(
            store, owner_id=OWNER, work_key="calendar", protocol=PROTOCOL, at=T0,
        )
        with pytest.raises(ValueError, match="already admitted"):
            admit_work(
                store, owner_id=OWNER, work_key="calendar",
                protocol=replace(PROTOCOL, cost_us=19), at=T0,
            )
        with store.transaction(owner_id=OWNER) as uow:
            entity = uow.get_entity(WORK_KIND, ident)
            entity.attributes["protocol"]["cost_us"] = 999
            uow.save_entity(entity)
        baseline = tuple(store.events())
        with pytest.raises(ValueError, match="original protocol event"):
            _run(store, "calendar", protocol=replace(PROTOCOL, cost_us=999))
        with pytest.raises(ValueError, match="admission/protocol"):
            _run(store, "calendar")
        assert tuple(store.events()) == baseline
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("fail_name", ("entity.state_transition", "od4.intake_decided"))
def test_late_append_crash_rolls_back_native_chart_state_command_and_paid_cost(
    adapter, fail_name, tmp_path, monkeypatch,
):
    store = _store(adapter, tmp_path)
    work_id = admit_work(
        store, owner_id=OWNER, work_key="failcase", protocol=PROTOCOL, at=T0,
    )
    original = MemoryUnitOfWork.append_event
    before = (store.entity(WORK_KIND, work_id), store.events())

    def fail(self, event):
        if event.name == fail_name:
            raise OSError("injected event append failure")
        return original(self, event)

    try:
        with monkeypatch.context() as mp:
            mp.setattr(MemoryUnitOfWork, "append_event", fail)
            with pytest.raises(OSError, match="injected event append"):
                _run(store, "failcase", action="reject")
        assert store.entity(WORK_KIND, work_id) == before[0]
        assert store.events() == before[1]
        committed = _run(store, "failcase", action="reject")
        assert committed.payload["after"] == "rejected"
        assert store.entity(WORK_KIND, work_id).attributes["paid_management_us"] == 11
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_management_causal_command_replay_rejects_missing_or_duplicate_receipt(
    adapter, tmp_path,
):
    store = _store(adapter, tmp_path)
    try:
        work_id = admit_work(
            store, owner_id=OWNER, work_key="audit", protocol=PROTOCOL, at=T0,
        )
        committed = _run(store, "audit")
        command_id = deterministic_id("od4-command-v1", OWNER, work_id, "observation:1")
        with store.transaction(owner_id=OWNER) as uow:
            command = uow.get_command(command_id)
            assert command is not None
            assert committed.causation_id == command_id
            assert command.payload["protocol"] == PROTOCOL.snapshot()
            uow.delete_command(command_id)
        with pytest.raises(ValueError, match="corresponding command"):
            _run(store, "audit")
    finally:
        if adapter == "sqlite":
            store.close()
