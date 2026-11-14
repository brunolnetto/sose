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
            with pytest.raises(ValueError, match={
                "wrong_command": "corresponding command",
                "duplicate_decision": "durable management cost ledger inconsistent",
                "missing_decision": "paid management projection contradicts durable ledger",
            }[mutation]):
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


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("field, value", (
    ("name", "reject"),
    ("entity_id", "work:other"),
    ("owner_id", "org:forged"),
    ("entity_type", "fake_entity_type"),
    ("due_at", T0 + timedelta(minutes=2)),
    ("issued_at", T0 + timedelta(minutes=2)),
    ("correlation_id", "correlation:wrong"),
))
def test_od4_replay_refuses_command_target_action_owner_and_time_forgery(
    adapter, field, value, tmp_path,
):
    from dataclasses import replace as dc_replace
    store = _store(adapter, tmp_path)
    try:
        ident = admit_work(
            store, owner_id=OWNER, work_key="command-check",
            protocol=PROTOCOL, at=T0,
        )
        committed = _run(store, "command-check")
        cmd_id = deterministic_id(
            "od4-command-v1", OWNER, ident, "observation:1",
        )
        with store.transaction(owner_id=OWNER) as uow:
            cmd = uow.get_command(cmd_id)
            assert cmd is not None
            uow.save_command(dc_replace(cmd, **{field: value}))
        before = store.events()
        with pytest.raises(ValueError, match="corresponding command"):
            _run(store, "command-check")
        assert store.events() == before
        assert store.entity(WORK_KIND, ident).attributes["paid_management_us"] == 11
        assert committed.payload["after"] == "accepted"
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("forgery", (
    "paid_projection", "missing_cause_registry",
    "decision_cost", "duplicate_cause",
))
def test_od4_second_paid_sham_reconciles_real_durable_decision_history(
    adapter, forgery, tmp_path,
):
    from dataclasses import replace as dc_replace
    store = _store(adapter, tmp_path)
    try:
        ident = admit_work(
            store, owner_id=OWNER, work_key="charge-check",
            protocol=PROTOCOL, at=T0,
        )
        first = _run(
            store, "charge-check", cause="paid:first", action="sham",
            matched="accept", at=T0 + timedelta(seconds=1),
        )
        with store.transaction(owner_id=OWNER) as uow:
            row = uow.get_entity(WORK_KIND, ident)
            if forgery == "paid_projection":
                row.attributes["paid_management_us"] = 0
                uow.save_entity(row)
            elif forgery == "missing_cause_registry":
                row.attributes["applied_causes"].clear()
                uow.save_entity(row)
            elif forgery == "decision_cost":
                uow._working.events.remove(first)
                uow.append_event(dc_replace(
                    first, payload={**first.payload, "cost_us": 12},
                ))
            else:
                uow.append_event(first)
        before = store.events()
        with pytest.raises(ValueError, match=(
            "paid management projection contradicts durable ledger"
            if forgery == "paid_projection" else
            "durable management cost ledger inconsistent"
        )):
            _run(
                store, "charge-check", cause="paid:next",
                action="sham", matched="reject",
                at=T0 + timedelta(seconds=2),
            )
        assert store.events() == before
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_od4_second_paid_sham_accumulates_exactly_and_survives_restart(
    adapter, tmp_path,
):
    store = _store(adapter, tmp_path)
    try:
        ident = admit_work(
            store, owner_id=OWNER, work_key="charge-positive",
            protocol=PROTOCOL, at=T0,
        )
        first = _run(
            store, "charge-positive", cause="paid:first",
            action="sham", matched="accept",
            at=T0 + timedelta(seconds=1),
        )
        second = _run(
            store, "charge-positive", cause="paid:second",
            action="sham", matched="reject",
            at=T0 + timedelta(seconds=2),
        )
        assert first.payload["paid_management_us"] == 11
        assert second.payload["paid_management_us"] == 22
        assert store.entity(WORK_KIND, ident).attributes["paid_management_us"] == 22
        assert _run(
            store, "charge-positive", cause="paid:second",
            action="sham", matched="reject",
            at=T0 + timedelta(seconds=2),
        ) == second
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(tmp_path / "management.sqlite") as reopened:
            assert reopened.entity(WORK_KIND, ident).attributes["paid_management_us"] == 22
            assert _run(
                reopened, "charge-positive", cause="paid:second",
                action="sham", matched="reject",
                at=T0 + timedelta(seconds=2),
            ) == second


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("mutation, expected", (
    ("accepted_back_to_pending", "projection contradicts durable ledger"),
    ("paid_sham_time_regressed", "projection contradicts durable ledger"),
    ("updated_at_forged", "projection contradicts durable ledger"),
    ("version_rewound", "projection contradicts durable ledger"),
    ("created_at_forged", "admission timestamp contradicts durable origin"),
))
def test_od4_projection_reconstructs_state_and_time_from_durable_history(
    adapter, mutation, expected, tmp_path,
):
    store = _store(adapter, tmp_path)
    try:
        key = "mutation:" + mutation
        identity = admit_work(
            store, owner_id=OWNER, work_key=key, protocol=PROTOCOL, at=T0,
        )
        action = ("sham" if mutation == "paid_sham_time_regressed" else "accept")
        first = _run(
            store, key, action=action,
            matched=("accept" if action == "sham" else None),
            at=T0 + timedelta(minutes=2),
        )
        assert first.payload["cost_us"] == 11
        with store.transaction(owner_id=OWNER) as uow:
            row = uow.get_entity(WORK_KIND, identity)
            if mutation == "accepted_back_to_pending":
                row.state = "pending"
            elif mutation == "paid_sham_time_regressed":
                row.attributes["last_at"] = T0.isoformat()
            elif mutation == "updated_at_forged":
                row.updated_at = T0
            elif mutation == "version_rewound":
                row.version = 0
            else:
                row.created_at = T0 + timedelta(minutes=1)
            uow.save_entity(row)
        before = tuple(store.events())
        with pytest.raises(ValueError, match=expected):
            _run(
                store, key, cause="second:event",
                action="reject" if mutation == "accepted_back_to_pending" else "sham",
                matched=None if mutation == "accepted_back_to_pending" else "reject",
                at=T0 + timedelta(minutes=1),
            )
        assert tuple(store.events()) == before
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_od4_sham_backdating_rejected_using_unmodified_durable_event(
    adapter, tmp_path,
):
    store = _store(adapter, tmp_path)
    try:
        key = "chronology"
        ident = admit_work(store, owner_id=OWNER, work_key=key,
                           protocol=PROTOCOL, at=T0)
        _run(store, key, action="sham", matched="accept",
             at=T0 + timedelta(minutes=2))
        with store.transaction(owner_id=OWNER) as uow:
            row = uow.get_entity(WORK_KIND, ident)
            row.attributes["last_at"] = T0.isoformat()
            uow.save_entity(row)
        with pytest.raises(ValueError, match="projection contradicts durable ledger"):
            _run(store, key, cause="backdated", action="sham",
                 matched="reject", at=T0 + timedelta(minutes=1))
        assert store.entity(WORK_KIND, ident).attributes["paid_management_us"] == 11
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_sham_replay_cannot_replace_original_pairing_by_rewriting_command_and_projection(
    adapter, tmp_path,
):
    from dataclasses import replace as dc_replace
    store = _store(adapter, tmp_path)
    try:
        key = "sham-pairing"
        identity = admit_work(
            store, owner_id=OWNER, work_key=key, protocol=PROTOCOL, at=T0,
        )
        original = _run(
            store, key, cause="paired:cause", action="sham", matched="accept",
        )
        command_id = deterministic_id(
            "od4-command-v1", OWNER, identity, "paired:cause",
        )
        before_event = original
        with store.transaction(owner_id=OWNER) as uow:
            row = uow.get_entity(WORK_KIND, identity)
            row.attributes["applied_causes"]["paired:cause"][1] = "reject"
            uow.save_entity(row)
            command = uow.get_command(command_id)
            altered_payload = {**command.payload}
            altered_payload["request"] = list(command.payload["request"])
            altered_payload["request"][1] = "reject"
            uow.save_command(dc_replace(command, payload=altered_payload))
        event_set = store.events()
        with pytest.raises(ValueError, match="durable management cost ledger inconsistent"):
            _run(
                store, key, cause="paired:cause", action="sham", matched="reject",
            )
        assert store.events() == event_set
        assert next(
            ev for ev in store.events()
            if ev.event_id == before_event.event_id
        ).payload["matched_action"] == "accept"
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_sham_unchanged_pairing_replays_after_two_charged_observations(
    adapter, tmp_path,
):
    store = _store(adapter, tmp_path)
    try:
        key = "sham-unchanged"
        identity = admit_work(store, owner_id=OWNER, work_key=key,
                              protocol=PROTOCOL, at=T0)
        first = _run(store, key, cause="c1", action="sham",
                     matched="accept", at=T0 + timedelta(seconds=1))
        second = _run(store, key, cause="c2", action="sham",
                      matched="reject", at=T0 + timedelta(seconds=2))
        before = store.events()
        assert _run(store, key, cause="c1", action="sham",
                    matched="accept", at=T0 + timedelta(seconds=1)) == first
        assert _run(store, key, cause="c2", action="sham",
                    matched="reject", at=T0 + timedelta(seconds=2)) == second
        assert store.events() == before
        assert store.entity(WORK_KIND, identity).attributes["paid_management_us"] == 22
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("action", ("accept", "reject"))
@pytest.mark.parametrize("tamper", (
    "missing", "duplicate", "other_target", "other_owner",
    "wrong_cause", "wrong_time", "wrong_correlation",
    "wrong_trigger", "wrong_before", "wrong_after",
))
def test_od4_real_intervention_replay_requires_unique_original_native_source(
    adapter, action, tamper, tmp_path,
):
    from dataclasses import replace as dc_replace

    store = _store(adapter, tmp_path)
    try:
        key = "native-source:" + action + ":" + tamper
        ident = admit_work(
            store, owner_id=OWNER, work_key=key,
            protocol=PROTOCOL, at=T0,
        )
        decision = _run(store, key, action=action)
        with store.transaction(owner_id=OWNER) as uow:
            sources = tuple(e for e in uow.events() if (
                e.name == "entity.state_transition"
                and e.entity_id == ident
            ))
            assert len(sources) == 1
            original = sources[0]
            if tamper == "duplicate":
                uow.append_event(original)
            else:
                uow._working.events.remove(original)
                replacement = {
                    "missing": None,
                    "other_target": dc_replace(original, entity_id="work:another"),
                    "other_owner": dc_replace(original, owner_id=OTHER),
                    "wrong_cause": dc_replace(original, causation_id="command:forged"),
                    "wrong_time": dc_replace(
                        original, occurred_at=T0 + timedelta(seconds=4),
                    ),
                    "wrong_correlation": dc_replace(
                        original, correlation_id="correlation:forged",
                    ),
                    "wrong_trigger": dc_replace(
                        original,
                        payload={**original.payload, "trigger": "other"},
                    ),
                    "wrong_before": dc_replace(
                        original,
                        payload={**original.payload, "from_state": "rejected"},
                    ),
                    "wrong_after": dc_replace(
                        original,
                        payload={**original.payload, "to_state": "pending"},
                    ),
                }[tamper]
                if replacement is not None:
                    uow.append_event(replacement)
        before = store.events()
        with pytest.raises(ValueError, match="native transition evidence inconsistent"):
            _run(store, key, action=action)
        assert store.events() == before
        assert store.entity(WORK_KIND, ident).attributes["paid_management_us"] == 11
        assert decision.payload["action"] == action
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_od4_sham_replay_rejects_fake_native_transition_for_same_command(
    adapter, tmp_path,
):
    from sose.core.events import DomainEvent
    store = _store(adapter, tmp_path)
    try:
        key = "sham-extra-native"
        ident = admit_work(store, owner_id=OWNER, work_key=key,
                           protocol=PROTOCOL, at=T0)
        committed = _run(store, key, action="sham", matched="accept")
        with store.transaction(owner_id=OWNER) as uow:
            uow.append_event(DomainEvent(
                event_id="forged-sham-native", name="entity.state_transition",
                entity_type=WORK_KIND, entity_id=ident,
                occurred_at=committed.occurred_at, owner_id=OWNER,
                causation_id=committed.causation_id,
                payload={"trigger": "accept",
                         "from_state": "pending", "to_state": "accepted"},
            ))
        with pytest.raises(ValueError, match="sham has unexpected native transition"):
            _run(store, key, action="sham", matched="accept")
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_od4_nested_outer_transaction_cannot_erase_committed_intake(adapter, tmp_path):
    store = _store(adapter, tmp_path)
    try:
        ident = admit_work(
            store, owner_id=OWNER, work_key="nested-case",
            protocol=PROTOCOL, at=T0,
        )
        initial_events = store.events()
        with store.transaction(owner_id=OWNER) as outer:
            assert store.transaction_depth == 1
            with pytest.raises(RuntimeError, match="nested transaction"):
                _run(store, "nested-case", action="accept")
            with pytest.raises(RuntimeError, match="nested transaction"):
                admit_work(
                    store, owner_id=OWNER, work_key="nested-admission",
                    protocol=PROTOCOL, at=T0,
                )
            assert outer.get_entity(WORK_KIND, ident).state == "pending"
        assert store.transaction_depth == 0
        assert store.events() == initial_events
        assert store.entity(WORK_KIND, ident).attributes["paid_management_us"] == 0
        effect = _run(store, "nested-case", action="accept")
        assert effect.payload["paid_management_us"] == PROTOCOL.cost_us
        assert store.entity(WORK_KIND, ident).state == "accepted"
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("tamper", (
    "removed", "duplicated", "wrong_owner", "wrong_target",
    "wrong_trigger", "wrong_previous_state", "wrong_next_state",
    "wrong_cause", "wrong_timestamp",
))
def test_od4_replay_requires_exactly_one_native_transition_receipt(
    adapter, tamper, tmp_path,
):
    from dataclasses import replace as dc_replace

    store = _store(adapter, tmp_path)
    try:
        key = "native:" + tamper
        work_id = admit_work(store, owner_id=OWNER, work_key=key,
                             protocol=PROTOCOL, at=T0)
        decision = _run(store, key, action="accept")
        with store.transaction(owner_id=OWNER) as uow:
            source = next(e for e in uow.events()
                          if e.event_id == decision.payload["source_event_id"])
            if tamper != "duplicated":
                uow._working.events.remove(source)
            if tamper == "duplicated":
                uow.append_event(source)
            elif tamper != "removed":
                changed = {}
                if tamper == "wrong_owner":
                    changed["owner_id"] = OTHER
                elif tamper == "wrong_target":
                    changed["entity_id"] = "wrong-work"
                elif tamper == "wrong_cause":
                    changed["causation_id"] = "wrong-command"
                elif tamper == "wrong_timestamp":
                    changed["occurred_at"] = T0 + timedelta(minutes=3)
                else:
                    payload = dict(source.payload)
                    key_name = {
                        "wrong_trigger": "trigger",
                        "wrong_previous_state": "from_state",
                        "wrong_next_state": "to_state",
                    }[tamper]
                    payload[key_name] = "forged"
                    changed["payload"] = payload
                uow.append_event(dc_replace(source, **changed))
        before = store.events()
        with pytest.raises(ValueError, match="durable native transition evidence inconsistent"):
            _run(store, key, action="accept")
        assert store.events() == before
        assert store.entity(WORK_KIND, work_id).state == "accepted"
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("mutation", (
    "missing", "foreign_owner", "backdated_command", "swapped_request", "wrong_action",
))
def test_subsequent_management_intervention_rejects_corrupted_original_command(
    adapter, mutation, tmp_path,
):
    """#464: the first paid sham cannot have its cause silently rewritten.

    The later intervention is permitted only if the earlier immutable-effect
    event still matches the original persisted causal Command, not merely
    the mutable projected aggregate cost.
    """
    store = _store(adapter, tmp_path)
    key = "management-command-fence"
    cause = "paid:sham:first"
    try:
        work_id = admit_work(
            store, owner_id=OWNER, work_key=key,
            protocol=PROTOCOL, at=T0,
        )
        original = _run(
            store, key, action="sham", matched="accept", cause=cause,
        )
        command_id = deterministic_id("od4-command-v1", OWNER, work_id, cause)
        with store.transaction(owner_id=OWNER) as uow:
            command = uow.get_command(command_id)
            assert command is not None
            if mutation == "missing":
                uow.delete_command(command_id)
            elif mutation == "foreign_owner":
                uow.save_command(replace(command, owner_id=OTHER))
            elif mutation == "backdated_command":
                uow.save_command(replace(
                    command, issued_at=T0 + timedelta(seconds=1),
                ))
            elif mutation == "swapped_request":
                rewritten = list(command.payload["request"])
                rewritten[1] = "reject"
                uow.save_command(replace(
                    command, payload={**command.payload, "request": rewritten},
                ))
            else:
                uow.save_command(replace(command, name="reject"))
        prior_events = tuple(store.events())
        prior_state = store.entity(WORK_KIND, work_id)
        with pytest.raises(ValueError, match="OD-4 durable management original Command"):
            _run(
                store, key, action="sham", matched="accept",
                cause="paid:sham:second", at=T0 + timedelta(minutes=2),
            )
        assert tuple(store.events()) == prior_events
        assert store.entity(WORK_KIND, work_id) == prior_state
        assert original in store.events()
    finally:
        if adapter == "sqlite":
            store.close()
