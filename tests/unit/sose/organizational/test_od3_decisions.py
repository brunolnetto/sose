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
    active = (replace(p, name=p.matched_policy, matched_policy=None)
              if p.name == "sham" else None)
    return decide_od3(owner_id=OWNER, queue=queue, policy=p, observed_at=NOW,
                      previous_decision_at=previous, observation_id=observation,
                      matched_active_policy=active)


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
    sham = decide(queue, policy("sham", matched_policy="cherry_picking"))
    assert sham.action == "SHAM"
    assert sham.before == sham.after
    assert sham.cost_us == active.cost_us == 3
    assert sham.selected_item_id == active.selected_item_id


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



def test_follow_up_skips_oldest_item_in_cooldown_but_contacts_next_eligible():
    oldest = replace(item("oldest", 20, 8),
                     last_contact_at=NOW - timedelta(microseconds=2))
    later = item("later", 10, 8)
    outcome = decide((oldest, later), policy("adaptive_follow_up"))
    assert outcome.action == "CONTACT"
    assert outcome.selected_item_id == "later"
    assert outcome.cost_us == 3


def test_follow_up_sham_matches_null_and_positive_cost_under_item_cooldown():
    cooling = (replace(item("oldest", 20, 8),
                       last_contact_at=NOW - timedelta(microseconds=2)),)
    active_policy = policy("adaptive_follow_up")
    sham_policy = policy("sham", matched_policy="adaptive_follow_up")
    active = decide(cooling, active_policy)
    sham = decide(cooling, sham_policy)
    assert (active.action, sham.action) == ("NOOP", "NOOP")
    assert active.cost_us == sham.cost_us == 0
    eligible = (item("overdue", 20, 8),)
    active = decide(eligible, active_policy)
    sham = decide(eligible, sham_policy)
    assert active.action == "CONTACT" and sham.action == "SHAM"
    assert active.selected_item_id == sham.selected_item_id
    assert active.cost_us == sham.cost_us == 3


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_matched_policy_decisions_share_durable_observation_identity(
    adapter, tmp_path,
):
    path = tmp_path / "od3-matched-sham.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    active = decide((item("slow", 10, 8), item("fast", 7, 1)),
                    policy("cherry_picking"), observation="pair:001")
    sham = decide((item("slow", 10, 8), item("fast", 7, 1)),
                  policy("sham", matched_policy="cherry_picking"), observation="pair:001")
    assert active.observation_id == sham.observation_id == "pair:001"
    assert active.decision_id != sham.decision_id
    try:
        ev_active, ev_sham = persist_od3_decision(store, active), persist_od3_decision(store, sham)
        assert ev_active.correlation_id == ev_sham.correlation_id
        assert ev_active.payload["observation_id"] == ev_sham.payload["observation_id"]
        assert ev_active.payload["matched_policy"] == ev_sham.payload["matched_policy"]
        assert ev_active.payload["policy"] != ev_sham.payload["policy"]
        with pytest.raises(ValueError, match="durable decision identity"):
            persist_od3_decision(store, replace(active, observation_id="pair:fraud"))
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as recovered:
            assert recovered.events() == (ev_active, ev_sham)
            assert persist_od3_decision(recovered, active) == ev_active
            assert persist_od3_decision(recovered, sham) == ev_sham


def test_undeclared_sham_target_is_rejected():
    with pytest.raises(ValueError, match="matched real policy"):
        policy("sham")
    with pytest.raises(ValueError, match="matched real policy"):
        policy("sham", matched_policy="unsupported")



@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_sham_controls_for_distinct_matched_policies_have_independent_identities(
    adapter, tmp_path,
):
    path = tmp_path / "two-sham-semantics.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    # Keep oldest overdue but just below starvation bound: shortest is eligible.
    queue = (item("slow", 19, 60), item("fast", 10, 1))
    follow = decide(
        queue, policy("sham", matched_policy="adaptive_follow_up"),
        observation="pair:both-policies",
    )
    picking = decide(
        queue, policy("sham", matched_policy="cherry_picking"),
        observation="pair:both-policies",
    )
    assert follow.decision_id != picking.decision_id
    assert follow.selected_item_id == "slow"
    assert picking.selected_item_id == "fast"
    try:
        one = persist_od3_decision(store, follow)
        two = persist_od3_decision(store, picking)
        assert one.event_id != two.event_id
        assert one.correlation_id == two.correlation_id
        assert one.payload["matched_policy"] != two.payload["matched_policy"]
        assert persist_od3_decision(store, follow) == one
        assert persist_od3_decision(store, picking) == two
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as restored:
            assert restored.events() == (one, two)

@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_sham_requires_matching_active_configuration_and_carries_identity(adapter, tmp_path):
    queue = (item("old", 12, 15), item("fast", 7, 2))
    active_policy = policy("adaptive_follow_up")
    mismatched_sham = policy("sham", matched_policy="adaptive_follow_up",
                             delay_threshold_us=15, decision_cost_us=99)
    with pytest.raises(ValueError, match="configuration mismatch"):
        decide_od3(owner_id=OWNER, queue=queue, policy=mismatched_sham,
                   observed_at=NOW, observation_id="pair:config",
                   matched_active_policy=active_policy)
    with pytest.raises(ValueError, match="explicit matched active"):
        decide_od3(owner_id=OWNER, queue=queue,
                   policy=policy("sham", matched_policy="adaptive_follow_up"),
                   observed_at=NOW, observation_id="pair:config")
    active = decide(queue, active_policy, observation="pair:config")
    sham = decide(queue, policy("sham", matched_policy="adaptive_follow_up"),
                  observation="pair:config")
    assert active.policy_config_id == sham.policy_config_id
    assert active.snapshot_id == sham.snapshot_id
    path = tmp_path / "sham-config.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    try:
        a, b = persist_od3_decision(store, active), persist_od3_decision(store, sham)
        assert a.correlation_id == b.correlation_id
        assert a.payload["policy_config_id"] == b.payload["policy_config_id"]
        assert a.payload["snapshot_id"] == b.payload["snapshot_id"]
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as restored:
            assert restored.events() == (a, b)


@pytest.mark.parametrize("mutation", ("clock", "service", "blocked", "contact", "prior"))
@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_reused_observation_id_does_not_pair_different_snapshots(
    mutation, adapter, tmp_path,
):
    queue = (item("old", 12, 15), item("fast", 7, 2))
    before = decide(queue, policy("adaptive_follow_up"), observation="reused")
    prior = None
    now = NOW
    if mutation == "clock":
        now = NOW + timedelta(microseconds=1)
    elif mutation == "service":
        queue = (replace(queue[0], service_us=16), queue[1])
    elif mutation == "blocked":
        queue = (replace(queue[0], blocked=True), queue[1])
    elif mutation == "contact":
        queue = (replace(queue[0], last_contact_at=NOW - timedelta(microseconds=1)), queue[1])
    else:
        prior = NOW - timedelta(microseconds=20)
    after = decide_od3(owner_id=OWNER, queue=queue,
                       policy=policy("sham", matched_policy="adaptive_follow_up"),
                       observed_at=now, previous_decision_at=prior,
                       observation_id="reused",
                       matched_active_policy=policy("adaptive_follow_up"))
    assert before.snapshot_id != after.snapshot_id
    assert before.decision_id != after.decision_id
    path = tmp_path / "snapshot-identity.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    try:
        a, b = persist_od3_decision(store, before), persist_od3_decision(store, after)
        assert a.correlation_id != b.correlation_id
        assert a.payload["observation_id"] == b.payload["observation_id"]
        assert a.payload["snapshot_id"] != b.payload["snapshot_id"]
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as restored:
            assert restored.events() == (a, b)


@pytest.mark.parametrize("bad_owner", ("", "  ", None))
def test_decision_rejects_invalid_owner_identity(bad_owner):
    with pytest.raises(ValueError, match="owner"):
        decide_od3(owner_id=bad_owner, queue=(), policy=policy("adaptive_follow_up"),
                   observed_at=NOW, observation_id="identity")


@pytest.mark.parametrize("bad_observation", ("", "  ", None))
def test_decision_rejects_invalid_observation_identity(bad_observation):
    with pytest.raises(ValueError, match="observation identity"):
        decide_od3(owner_id=OWNER, queue=(), policy=policy("adaptive_follow_up"),
                   observed_at=NOW, observation_id=bad_observation)


def test_decision_rejects_mutable_queue_and_future_prior_decision():
    with pytest.raises(ValueError, match="immutable tuple"):
        decide_od3(owner_id=OWNER, queue=[], policy=policy("adaptive_follow_up"),
                   observed_at=NOW, observation_id="queue")
    with pytest.raises(ValueError, match="future"):
        decide((item("old", 15, 2),), policy("adaptive_follow_up"),
               previous=NOW + timedelta(microseconds=1))


@pytest.mark.parametrize("bad", ("negative", "boolean", "starvation"))
def test_policy_rejects_invalid_numeric_configuration(bad):
    changes = {
        "negative": {"decision_cost_us": -1},
        "boolean": {"cooldown_us": True},
        "starvation": {"starvation_bound_us": 4},
    }
    with pytest.raises(ValueError):
        policy("adaptive_follow_up", **changes[bad])


def test_sham_rejects_wrong_matched_active_policy_and_active_rejects_override():
    q = (item("job", 12, 2),)
    with pytest.raises(ValueError, match="explicit matched active"):
        decide_od3(owner_id=OWNER, queue=q,
                   policy=policy("sham", matched_policy="cherry_picking"),
                   matched_active_policy=policy("adaptive_follow_up"),
                   observed_at=NOW, observation_id="wrong-active")
    with pytest.raises(ValueError, match="active policy cannot"):
        decide_od3(owner_id=OWNER, queue=q, policy=policy("cherry_picking"),
                   matched_active_policy=policy("adaptive_follow_up"),
                   observed_at=NOW, observation_id="wrong-active")


@pytest.mark.parametrize("attribute", ("arrived_at", "last_contact_at"))
def test_work_rejects_naive_datetime(attribute):
    q = item("job", 10, 2)
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(q, **{attribute: NOW.replace(tzinfo=None)})


def test_work_rejects_nonpositive_and_boolean_effort():
    q = item("job", 10, 2)
    for bad in (0, -1, True):
        with pytest.raises(ValueError, match="positive integer"):
            replace(q, service_us=bad)


def test_queue_all_blocked_produces_noop_without_cost():
    d = decide((item("blocked", 30, 3, blocked=True),), policy("cherry_picking"))
    assert d.action == "NOOP" and d.cost_us == 0 and d.selected_item_id is None
    assert d.before == d.after


def test_follow_up_does_not_select_not_yet_overdue_item():
    d = decide((item("young", 1, 2),), policy("adaptive_follow_up"))
    assert d.action == "NOOP" and d.cost_us == 0


def test_snapshot_timestamp_requires_timezone_awareness():
    with pytest.raises(ValueError, match="timezone-aware"):
        decide_od3(owner_id=OWNER, queue=(), policy=policy("adaptive_follow_up"),
                   observed_at=NOW.replace(tzinfo=None), observation_id="naive")


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("mutation", (
    "wrong_action", "phantom_selection", "fake_priority_order",
    "duplicate_queue_id", "swapped_membership", "negative_cost",
    "boolean_cost", "charged_noop", "reordered_sham",
    "reordered_contact", "invalid_policy", "unrecognized_action",
    "empty_reason",
))
def test_durable_od3_evidence_rejects_contradictory_claims_before_append(
    adapter, mutation, tmp_path,
):
    """Diagnostic evidence cannot imply impossible queue effects or costs (#463)."""
    queue = (item("old", 18, 100), item("fast", 8, 1))
    active = decide(queue, policy("cherry_picking"), observation="invalid-evidence")
    assert active.selected_item_id == "fast"
    sham = decide(queue, policy("sham", matched_policy="cherry_picking"),
                  observation="invalid-evidence")
    follow_up = decide(queue, policy("adaptive_follow_up"),
                       observation="invalid-evidence")
    bad = {
        "wrong_action": replace(active, action="CONTACT"),
        "phantom_selection": replace(active, selected_item_id="unknown"),
        "fake_priority_order": replace(active, after=active.before),
        "duplicate_queue_id": replace(active, before=("old", "old")),
        "swapped_membership": replace(active, after=("fast", "unknown")),
        "negative_cost": replace(active, cost_us=-1),
        "boolean_cost": replace(active, cost_us=True),
        "charged_noop": replace(active, action="NOOP", selected_item_id=None,
                                after=active.before),
        "reordered_sham": replace(sham, after=active.after),
        "reordered_contact": replace(follow_up, after=active.after),
        "invalid_policy": replace(active, policy="unsupported"),
        "unrecognized_action": replace(active, action="DONE"),
        "empty_reason": replace(active, reason=""),
    }[mutation]
    path = tmp_path / "od3-invalid-evidence.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    try:
        with pytest.raises(ValueError, match="OD-3 decision evidence"):
            persist_od3_decision(store, bad)
        assert store.events() == ()
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as reopened:
            assert reopened.events() == ()
            with pytest.raises(ValueError, match="OD-3 decision evidence"):
                persist_od3_decision(reopened, bad)



def test_repeated_follow_up_never_starves_uncontacted_work_after_cooldown():
    """Oldest contacted work cannot monopolize an operational contact budget."""
    oldest = replace(item("old", 100, 90),
                     last_contact_at=NOW - timedelta(microseconds=11))
    never_contacted = item("new", 15, 20)
    active = decide((oldest, never_contacted), policy("adaptive_follow_up"))
    sham = decide((oldest, never_contacted),
                  policy("sham", matched_policy="adaptive_follow_up"))
    assert active.selected_item_id == sham.selected_item_id == "new"
    assert active.action == "CONTACT" and sham.action == "SHAM"
    assert active.cost_us == sham.cost_us == 3
    both_contacted = (
        replace(oldest, last_contact_at=NOW - timedelta(microseconds=13)),
        replace(never_contacted, last_contact_at=NOW - timedelta(microseconds=17)),
    )
    after = decide(both_contacted, policy("adaptive_follow_up"))
    assert after.selected_item_id == "new"  # least-recently-contacted


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_v2_fair_follow_up_identity_never_reuses_legacy_v1_evidence(
    adapter, tmp_path,
):
    from sose.core.events import DomainEvent
    from sose.core.identity import deterministic_id

    queue = (replace(item("old", 40, 80),
                     last_contact_at=NOW - timedelta(microseconds=11)),
             item("uncontacted", 15, 5))
    p = policy("adaptive_follow_up")
    fresh = decide(queue, p, observation="historical:contact")
    old_id = deterministic_id(
        "od3-decision", OWNER, fresh.observation_id,
        fresh.policy, fresh.matched_policy,
        fresh.snapshot_id, fresh.policy_config_id,
    )
    assert old_id != fresh.decision_id
    assert fresh.selected_item_id == "uncontacted"
    path = tmp_path / "legacy-fairness.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    legacy = DomainEvent(
        event_id=old_id, name="od3.policy_decided",
        entity_type="organizational_policy", entity_id=OWNER,
        occurred_at=NOW, owner_id=OWNER,
        payload={"reason": "oldest contactable overdue work",
                 "selected_item_id": "old"},
    )
    try:
        with store.transaction(owner_id=OWNER) as uow:
            uow.append_event(legacy)
        applied = persist_od3_decision(store, fresh)
        assert applied.event_id != legacy.event_id
        assert applied.payload["selected_item_id"] == "uncontacted"
        assert store.events() == (legacy, applied)
        assert persist_od3_decision(store, fresh) == applied
        assert store.events() == (legacy, applied)
        # Cherry-picking did not change semantics and retains v1 identity.
        picking = decide(queue, policy("cherry_picking"),
                         observation="historical:cherry")
        assert picking.decision_id == deterministic_id(
            "od3-decision", OWNER, picking.observation_id,
            picking.policy, picking.matched_policy,
            picking.snapshot_id, picking.policy_config_id,
        )
        placebo = decide(queue, policy("sham", matched_policy="adaptive_follow_up"),
                         observation="historical:contact")
        assert placebo.decision_id != deterministic_id(
            "od3-decision", OWNER, placebo.observation_id,
            placebo.policy, placebo.matched_policy,
            placebo.snapshot_id, placebo.policy_config_id,
        )
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as reopened:
            assert reopened.events() == (legacy, applied)
            assert persist_od3_decision(reopened, fresh) == applied
