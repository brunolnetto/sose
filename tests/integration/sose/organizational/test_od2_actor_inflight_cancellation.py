"""#462 RED: explicit, paid actor-local in-flight cancel before exit.

This does not authorize cancellation of a business-domain ScheduledWork, nor
release an authoritative PhysicalReservation. It covers the actor's own
assigned_work_id projection with causal Command/Event and durable restart.
"""
from datetime import datetime, timedelta, timezone

import pytest

from sose.organizational.od2_actor import ACTOR_KIND, seed_actor, transition_actor
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence


T0 = datetime(2026, 10, 18, 8, tzinfo=timezone.utc)
OWNER = "org:actors-cancel"
MODEL = "actor-cancel-v1"


def _transition(store, actor_id, action, hour, *, work_id=None, cost_us=0,
                cause_id=None, owner_id=OWNER):
    return transition_actor(
        store, owner_id=owner_id, actor_id=actor_id, action=action,
        cause_id=cause_id or ("actor:" + action + ":" + str(hour)),
        at=T0 + timedelta(hours=hour), model_spec_hash=MODEL, config_epoch=1,
        work_id=work_id, cost_us=cost_us,
    )


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_paid_cancellation_frees_actor_assignment_before_exit_with_restart(adapter, tmp_path):
    path = tmp_path / "actor-cancel.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    try:
        actor_id = seed_actor(
            store, owner_id=OWNER, actor_key="cancelled", model_spec_hash=MODEL,
            config_epoch=1, at=T0,
        )
        _transition(store, actor_id, "activate", 1)
        _transition(store, actor_id, "assign", 2, work_id="work:critical")
        with pytest.raises(Exception):
            _transition(store, actor_id, "exit", 3)
        assert store.entity(ACTOR_KIND, actor_id).attributes["assigned_work_id"] == "work:critical"
        before = tuple(store.events())
        cancelled = _transition(
            store, actor_id, "cancel_assigned", 3,
            work_id="work:critical", cost_us=750000,
        )
        assert cancelled.state == "eligible"
        assert cancelled.attributes["assigned_work_id"] is None
        assert cancelled.attributes["paid_transition_us"] == 750000
        assert cancelled.attributes["cancelled_work_ids"] == ["work:critical"]
        assert cancelled.attributes["time_us"]["active"] == 3600 * 1000000
        added = [e for e in store.events() if e.event_id not in {p.event_id for p in before}]
        assert len(added) == 1
        event = added[0]
        assert event.payload["trigger"] == "cancel_assigned"
        assert event.owner_id == OWNER
        assert store.command(event.causation_id).name == "cancel_assigned"
        assert store.command(event.causation_id).payload["external_cause_id"] == "actor:cancel_assigned:3"
        assert store.command(event.causation_id).payload["cancelled_work_id"] == "work:critical"
        assert store.command(event.causation_id).payload["charged_cost_us"] == 750000
        _transition(store, actor_id, "exit", 4)
        assert store.entity(ACTOR_KIND, actor_id).state == "exited"
        before = tuple(store.events())
        _transition(store, actor_id, "cancel_assigned", 3,
                    work_id="work:critical", cost_us=750000)
        assert tuple(store.events()) == before
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as recovered:
            before = tuple(recovered.events())
            _transition(recovered, actor_id, "cancel_assigned", 3,
                        work_id="work:critical", cost_us=750000)
            assert tuple(recovered.events()) == before
            subject = recovered.entity(ACTOR_KIND, actor_id)
            assert subject.state == "exited"
            assert subject.attributes["assigned_work_id"] is None
            assert subject.attributes["cancelled_work_ids"] == ["work:critical"]


@pytest.mark.parametrize("work_id,cost_us,match", [
    (None, 750000, "matching assigned work"),
    ("other", 750000, "matching assigned work"),
    ("work:critical", 0, "paid"),
])
def test_actor_cannot_cancel_without_fenced_work_id_and_paid_cost(work_id, cost_us, match):
    store = MemoryPersistence()
    actor_id = seed_actor(store, owner_id=OWNER, actor_key="negative",
                          model_spec_hash=MODEL, config_epoch=1, at=T0)
    _transition(store, actor_id, "activate", 1)
    _transition(store, actor_id, "assign", 2, work_id="work:critical")
    before_actor = store.entity(ACTOR_KIND, actor_id)
    before_events = tuple(store.events())
    with pytest.raises(ValueError, match=match):
        _transition(store, actor_id, "cancel_assigned", 3,
                    work_id=work_id, cost_us=cost_us)
    assert store.entity(ACTOR_KIND, actor_id) == before_actor
    assert tuple(store.events()) == before_events
    assert store.entity(ACTOR_KIND, actor_id).attributes["paid_transition_us"] == 0


def test_actor_cancel_rejects_foreign_owner_and_cause_reuse_without_mutation():
    store = MemoryPersistence()
    actor_id = seed_actor(store, owner_id=OWNER, actor_key="owner-fence",
                          model_spec_hash=MODEL, config_epoch=1, at=T0)
    _transition(store, actor_id, "activate", 1)
    _transition(store, actor_id, "assign", 2, work_id="work:critical")
    before = tuple(store.events())
    with pytest.raises(PermissionError, match="another operational owner"):
        _transition(store, actor_id, "cancel_assigned", 3,
                    owner_id="org:forged", work_id="work:critical", cost_us=1)
    assert tuple(store.events()) == before
    _transition(store, actor_id, "cancel_assigned", 3,
                work_id="work:critical", cost_us=750000)
    committed = tuple(store.events())
    with pytest.raises(ValueError, match="different action"):
        _transition(store, actor_id, "complete", 3, work_id="work:critical",
                    cause_id="actor:cancel_assigned:3")
    with pytest.raises(ValueError, match="different action"):
        _transition(store, actor_id, "cancel_assigned", 3, work_id="work:critical",
                    cost_us=1, cause_id="actor:cancel_assigned:3")
    assert tuple(store.events()) == committed
