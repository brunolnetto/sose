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


@pytest.mark.parametrize("invalid, expected", (
    ({"model_spec_hash": " "}, "canonical nonblank"),
    ({"protocol_id": ""}, "canonical nonblank"),
    ({"config_epoch": -1}, "nonnegative"),
    ({"config_epoch": True}, "nonnegative"),
    ({"cost_us": 0}, "positive integer"),
    ({"cost_us": True}, "positive integer"),
))
def test_management_protocol_rejects_bad_preregistration(invalid, expected):
    with pytest.raises(ValueError, match=expected):
        replace(PROTOCOL, **invalid)


@pytest.mark.parametrize("operation", ("admit", "intervene"))
@pytest.mark.parametrize("field", ("owner_id", "work_key", "at", "protocol"))
def test_management_request_rejects_untyped_blank_or_unbounded_inputs(
    operation, field,
):
    store = MemoryPersistence()
    if operation == "admit":
        params = dict(owner_id=OWNER, work_key="case", protocol=PROTOCOL, at=T0)
        params[field] = (T0.replace(tzinfo=None) if field == "at"
                         else object() if field == "protocol" else " ")
        expected = ("timezone-aware" if field == "at"
                    else "typed protocol" if field == "protocol"
                    else "canonical nonblank")
        with pytest.raises((ValueError, TypeError), match=expected):
            admit_work(store, **params)
    else:
        admit_work(store, owner_id=OWNER, work_key="case",
                   protocol=PROTOCOL, at=T0)
        params = dict(owner_id=OWNER, work_key="case",
                      protocol=PROTOCOL, cause_id="sample",
                      action="accept", at=T0 + timedelta(seconds=1))
        params[field] = (T0.replace(tzinfo=None) if field == "at"
                         else None if field == "protocol" else " ")
        expected = ("timezone-aware" if field == "at"
                    else "typed protocol" if field == "protocol"
                    else "canonical nonblank")
        baseline = store.events()
        with pytest.raises((ValueError, TypeError), match=expected):
            intervene_work(store, **params)
        assert store.events() == baseline


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("origin_mutation", (
    "missing", "duplicate", "foreign_owner", "wrong_payload",
))
def test_management_origin_evidence_is_immutable_and_unique(
    adapter, origin_mutation, tmp_path,
):
    from dataclasses import replace as dc_replace
    store = _store(adapter, tmp_path)
    try:
        work_id = admit_work(
            store, owner_id=OWNER, work_key="origin", protocol=PROTOCOL, at=T0,
        )
        origin_id = deterministic_id("od4-work-admission-v1", work_id)
        with store.transaction(owner_id=OWNER) as uow:
            source = next(e for e in uow.events() if e.event_id == origin_id)
            if origin_mutation != "duplicate":
                uow._working.events.remove(source)
            if origin_mutation == "duplicate":
                uow.append_event(source)
            elif origin_mutation == "foreign_owner":
                uow.append_event(dc_replace(source, owner_id=OTHER))
            elif origin_mutation == "wrong_payload":
                uow.append_event(dc_replace(
                    source, payload={**source.payload, "protocol": {"forged": True}},
                ))
        baseline = store.events()
        with pytest.raises(ValueError, match="original protocol event"):
            _run(store, "origin")
        assert store.events() == baseline
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("mutation", (
    "duplicate_decision", "missing_decision", "wrong_command", "orphan_identity",
))
def test_replay_and_fresh_intervention_require_single_causal_identity(
    adapter, mutation, tmp_path,
):
    from dataclasses import replace as dc_replace
    from sose.core.events import DomainEvent, Command

    store = _store(adapter, tmp_path)
    try:
        work_id = admit_work(
            store, owner_id=OWNER, work_key="replay", protocol=PROTOCOL, at=T0,
        )
        if mutation == "orphan_identity":
            conflict = DomainEvent(
                event_id=deterministic_id(
                    "od4-intake-v1", OWNER, work_id, "observation:1",
                ),
                name="untrusted.fake_event", entity_type=WORK_KIND,
                entity_id=work_id, owner_id=OWNER, occurred_at=T0,
            )
            with store.transaction(owner_id=OWNER) as uow:
                uow.append_event(conflict)
            with pytest.raises(ValueError, match="cause identity already used"):
                _run(store, "replay")
        else:
            decision = _run(store, "replay")
            with store.transaction(owner_id=OWNER) as uow:
                if mutation == "duplicate_decision":
                    uow.append_event(decision)
                elif mutation == "missing_decision":
                    uow._working.events.remove(decision)
                elif mutation == "wrong_command":
                    command_id = deterministic_id(
                        "od4-command-v1", OWNER, work_id, "observation:1",
                    )
                    cmd = uow.get_command(command_id)
                    uow.save_command(dc_replace(
                        cmd, payload={**cmd.payload, "request": ["forged"]},
                    ))
            with pytest.raises(ValueError, match=(
                "unique causal decision" if mutation != "wrong_command"
                else "corresponding command"
            )):
                _run(store, "replay")
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_management_rejects_retrograde_sham_after_prior_paid_observation(adapter, tmp_path):
    store = _store(adapter, tmp_path)
    try:
        admit_work(store, owner_id=OWNER, work_key="time", protocol=PROTOCOL, at=T0)
        first = _run(
            store, "time", action="sham", matched="reject",
            at=T0 + timedelta(minutes=2),
        )
        with pytest.raises(ValueError, match="time regressed"):
            _run(
                store, "time", cause="older", action="sham", matched="accept",
                at=T0 + timedelta(minutes=1),
            )
        with pytest.raises(ValueError, match="sham pairing"):
            _run(
                store, "time", cause="wrong-match",
                action="accept", matched="outside",
            )
        assert first.payload["cost_us"] == 11
        assert len(tuple(e for e in store.events() if e.name == "od4.intake_decided")) == 1
    finally:
        if adapter == "sqlite":
            store.close()
