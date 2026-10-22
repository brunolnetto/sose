"""#462: assigned work and paid actor cost require committed causal identity."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sose.core.identity import deterministic_id
from sose.organizational.od2_actor import ACTOR_KIND, seed_actor, transition_actor
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence

T0 = datetime(2026, 10, 22, 9, tzinfo=timezone.utc)
OWNER = "org:actor-work-evidence"
MODEL = "actor-model:work-v1"
CAUSE = "assigned:job-1"


def _store(adapter, tmp_path):
    return MemoryPersistence() if adapter == "memory" else SQLitePersistence(
        tmp_path / "causal-work.sqlite",
    )


def _transition(store, actor_id, *, action, cause, hours, work_id=None, cost_us=0):
    return transition_actor(
        store, owner_id=OWNER, actor_id=actor_id, action=action,
        cause_id=cause, at=T0 + timedelta(hours=hours),
        model_spec_hash=MODEL, config_epoch=1, work_id=work_id, cost_us=cost_us,
    )


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_paid_work_identity_is_anchored_in_original_command_and_statechart_event(
    adapter, tmp_path,
):
    store = _store(adapter, tmp_path)
    try:
        actor_id = seed_actor(
            store, owner_id=OWNER, actor_key="actor-1",
            model_spec_hash=MODEL, config_epoch=1, at=T0,
        )
        _transition(store, actor_id, action="activate", cause="activate", hours=1)
        assigned = _transition(
            store, actor_id, action="assign", cause=CAUSE,
            hours=2, work_id="work:job-1", cost_us=7,
        )
        command_id = deterministic_id("od2-actor-command", OWNER, actor_id, CAUSE)
        with store.transaction(owner_id=OWNER) as uow:
            command = uow.get_command(command_id)
            assert command is not None
            assert command.payload == {
                "evidence_version": 1, "external_cause_id": CAUSE,
                "work_id": "work:job-1", "cost_us": 7,
                "model_spec_hash": MODEL, "config_epoch": 1,
            }
            source = tuple(event for event in uow.events()
                           if event.causation_id == command_id)
            assert len(source) == 1
            assert source[0].name == "entity.state_transition"
            assert source[0].payload["trigger"] == "assign"
            assert source[0].correlation_id == command.correlation_id
            work_evidence = tuple(ev for ev in uow.events() if (
                ev.name == "od2.actor_work_caused"
                and ev.payload.get("source_event_id") == source[0].event_id
            ))
            assert len(work_evidence) == 1
            assert work_evidence[0].payload["work_id"] == "work:job-1"
            assert work_evidence[0].payload["cost_us"] == 7
            assert work_evidence[0].causation_id == source[0].event_id
        before = (assigned.version, assigned.attributes["time_us"].copy(),
                  assigned.attributes["paid_transition_us"], store.events())
        replay = _transition(
            store, actor_id, action="assign", cause=CAUSE,
            hours=2, work_id="work:job-1", cost_us=7,
        )
        assert replay.version == before[0]
        assert replay.attributes["time_us"] == before[1]
        assert replay.attributes["paid_transition_us"] == before[2]
        assert store.events() == before[3]
        with pytest.raises(ValueError, match="different action"):
            _transition(
                store, actor_id, action="assign", cause=CAUSE,
                hours=2, work_id="work:another", cost_us=7,
            )
        with pytest.raises(ValueError, match="different action"):
            _transition(
                store, actor_id, action="assign", cause=CAUSE,
                hours=2, work_id="work:job-1", cost_us=8,
            )
        assert store.events() == before[3]
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(tmp_path / "causal-work.sqlite") as reopened:
            assert _transition(
                reopened, actor_id, action="assign", cause=CAUSE,
                hours=2, work_id="work:job-1", cost_us=7,
            ).attributes["assigned_work_id"] == "work:job-1"


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("mutation", ("missing_command", "swapped_work",
                                     "deleted_source", "duplicate_source"))
def test_actor_replay_refuses_mutable_projection_without_valid_causal_evidence(
    adapter, mutation, tmp_path,
):
    store = _store(adapter, tmp_path)
    try:
        actor_id = seed_actor(
            store, owner_id=OWNER, actor_key="actor-replay",
            model_spec_hash=MODEL, config_epoch=1, at=T0,
        )
        _transition(store, actor_id, action="activate", cause="activate", hours=1)
        _transition(
            store, actor_id, action="assign", cause=CAUSE,
            hours=2, work_id="work:job-1", cost_us=7,
        )
        command_id = deterministic_id("od2-actor-command", OWNER, actor_id, CAUSE)
        with store.transaction(owner_id=OWNER) as uow:
            command = uow.get_command(command_id)
            source = next(ev for ev in uow.events() if ev.causation_id == command_id)
            if mutation == "missing_command":
                uow.delete_command(command_id)
            elif mutation == "swapped_work":
                uow.save_command(replace(
                    command, payload={**command.payload, "work_id": "work:forged"},
                ))
            elif mutation == "deleted_source":
                uow._working.events.remove(source)
            else:
                uow.append_event(source)
        before = store.entity(ACTOR_KIND, actor_id)
        with pytest.raises(ValueError, match="durable Command|durable StateChart"):
            _transition(
                store, actor_id, action="assign", cause=CAUSE,
                hours=2, work_id="work:job-1", cost_us=7,
            )
        after = store.entity(ACTOR_KIND, actor_id)
        assert after == before
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_forged_applied_cause_without_durable_command_cannot_bypass_execution(
    adapter, tmp_path,
):
    store = _store(adapter, tmp_path)
    try:
        actor_id = seed_actor(
            store, owner_id=OWNER, actor_key="ghost",
            model_spec_hash=MODEL, config_epoch=1, at=T0,
        )
        _transition(store, actor_id, action="activate", cause="activate", hours=1)
        at = T0 + timedelta(hours=2)
        with store.transaction(owner_id=OWNER) as uow:
            fake = uow.get_entity(ACTOR_KIND, actor_id)
            fake.attributes["applied_causes"]["ghost:assign"] = [
                "assign", at.isoformat(), "work:ghost", 0,
            ]
            fake.attributes["assigned_work_id"] = "work:ghost"
            uow.save_entity(fake)
        before = store.events()
        with pytest.raises(ValueError, match="canonical durable Command"):
            _transition(
                store, actor_id, action="assign", cause="ghost:assign",
                hours=2, work_id="work:ghost",
            )
        assert store.events() == before
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("mutation", (
    "forge_both_command_and_projection", "duplicate_evidence", "missing_evidence",
))
def test_actor_replay_requires_unambiguous_append_only_work_and_cost_evidence(
    adapter, mutation, tmp_path,
):
    """P1: equal mutable Command/projection cannot rewrite original work facts."""
    store = _store(adapter, tmp_path)
    try:
        actor_id = seed_actor(
            store, owner_id=OWNER, actor_key="immutable-source",
            model_spec_hash=MODEL, config_epoch=1, at=T0,
        )
        _transition(store, actor_id, action="activate", cause="activate", hours=1)
        _transition(
            store, actor_id, action="assign", cause=CAUSE,
            hours=2, work_id="work:original", cost_us=7,
        )
        cmd_id = deterministic_id("od2-actor-command", OWNER, actor_id, CAUSE)
        with store.transaction(owner_id=OWNER) as uow:
            originals = tuple(e for e in uow.events() if (
                e.name == "od2.actor_work_caused" and e.entity_id == cmd_id
            ))
            assert len(originals) == 1
            original = originals[0]
            if mutation == "forge_both_command_and_projection":
                command = uow.get_command(cmd_id)
                uow.save_command(replace(
                    command,
                    payload={**command.payload, "work_id": "work:forged", "cost_us": 999},
                ))
                actor = uow.get_entity(ACTOR_KIND, actor_id)
                actor.attributes["assigned_work_id"] = "work:forged"
                actor.attributes["paid_transition_us"] = 999
                actor.attributes["applied_causes"][CAUSE] = [
                    "assign", (T0 + timedelta(hours=2)).isoformat(), "work:forged", 999,
                ]
                uow.save_entity(actor)
            elif mutation == "duplicate_evidence":
                uow.append_event(original)
            else:
                uow._working.events.remove(original)
        original_count = len(store.events())
        with pytest.raises(ValueError, match="immutable work/cost causal evidence"):
            _transition(
                store, actor_id, action="assign", cause=CAUSE,
                hours=2, work_id=(
                    "work:forged" if mutation == "forge_both_command_and_projection"
                    else "work:original"
                ), cost_us=(999 if mutation == "forge_both_command_and_projection" else 7),
            )
        assert len(store.events()) == original_count
    finally:
        if adapter == "sqlite":
            store.close()
