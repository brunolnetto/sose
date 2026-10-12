"""OD-5a boundary reconciler fail-closed dispatcher contracts.

Pure fault injection at the service boundary covers causal identity checks
which full happy-path physical executions do not deliberately violate.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import sose.organizational.od5a_reconciler as rec
from sose.composition.boundary import StaleBoundaryClaimError


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
LEASE = timedelta(minutes=2)


@pytest.mark.parametrize(("job_id", "owner_id", "clock", "lease", "reason"), [
    ("", "worker", NOW, LEASE, "job and owner identities"),
    ("org", "", NOW, LEASE, "job and owner identities"),
    ("org", "worker", datetime(2026, 1, 1), LEASE, "timezone-aware"),
    ("org", "worker", NOW, timedelta(0), "positive delivery lease"),
    ("org", "worker", NOW, timedelta(seconds=-1), "positive delivery lease"),
])
def test_reconciler_rejects_invalid_worker_or_clock_before_touching_store(
    monkeypatch, job_id, owner_id, clock, lease, reason,
):
    def must_not_read(*args, **kwargs):
        raise AssertionError("invalid input touched durable state")
    monkeypatch.setattr(rec, "_plan_one", must_not_read)
    with pytest.raises(ValueError, match=reason):
        rec.reconcile_od5a_boundary_step(
            object(), job_id=job_id, owner_id=owner_id,
            now=clock, lease_duration=lease,
        )


@pytest.mark.parametrize(("action", "advanced"), [
    ("published", True), ("claimed", True), ("acknowledged", True),
    ("materialized", True), ("certified", True), ("blocked", False),
    ("idle", False), ("complete", False),
])
def test_reconciler_progress_flag_excludes_noop_and_blocked(action, advanced):
    assert rec.OD5aReconcileStep("org", action).advanced is advanced


def _dispatch(monkeypatch, action):
    plan = rec._Plan(
        action, event_id="committed-event", message_id="canonical-boundary",
        effect_id="certified-effect",
    )
    monkeypatch.setattr(rec, "_plan_one", lambda *args, **kwargs: plan)
    return lambda: rec.reconcile_od5a_boundary_step(
        object(), job_id="org", owner_id="worker",
        now=NOW, lease_duration=LEASE,
    )


def test_publication_refuses_message_identity_substitution(monkeypatch):
    run = _dispatch(monkeypatch, "published")
    monkeypatch.setattr(
        rec, "publish_od5a_physical_completion",
        lambda *args, **kwargs: SimpleNamespace(message_id="another-message"),
    )
    with pytest.raises(RuntimeError, match="causal identity"):
        run()


def test_claim_refuses_cross_organization_or_unavailable_delivery(monkeypatch):
    run = _dispatch(monkeypatch, "claimed")
    class NoClaim:
        def __init__(self, *args):
            pass
        def claim_next(self, **kwargs):
            return None
    monkeypatch.setattr(rec, "BoundaryService", NoClaim)
    assert run().action == "blocked"

    class AlienClaim(NoClaim):
        def claim_next(self, **kwargs):
            return SimpleNamespace(message_id="other")
    monkeypatch.setattr(rec, "BoundaryService", AlienClaim)
    with pytest.raises(RuntimeError, match="organization or event identity"):
        run()


def test_stale_delivery_ack_cannot_materialize_a_command(monkeypatch):
    run = _dispatch(monkeypatch, "acknowledged")
    def stale(*args, **kwargs):
        raise StaleBoundaryClaimError("superseded")
    monkeypatch.setattr(rec, "consume_od5a_physical_completion", stale)
    assert run().action == "blocked"


def test_ack_rejects_changed_message_or_command_identity(monkeypatch):
    run = _dispatch(monkeypatch, "acknowledged")
    for consumption in (
        SimpleNamespace(message_id="alien", consumer_effect_id="certified-effect"),
        SimpleNamespace(message_id="canonical-boundary", consumer_effect_id="other"),
    ):
        monkeypatch.setattr(
            rec, "consume_od5a_physical_completion",
            lambda *args, value=consumption, **kwargs: value,
        )
        with pytest.raises(RuntimeError, match="unexpected downstream Command"):
            run()


def test_observation_cannot_swap_the_committed_physical_event(monkeypatch):
    run = _dispatch(monkeypatch, "materialized")
    monkeypatch.setattr(
        rec, "apply_od5a_physical_observation",
        lambda *args, **kwargs: SimpleNamespace(id="other-event"),
    )
    with pytest.raises(RuntimeError, match="physical event identity"):
        run()


@pytest.mark.parametrize(("entity_id", "effect_id"), [
    ("other", "certified-effect"), ("committed-event", "other"),
])
def test_certificate_cannot_rebind_event_or_effect(monkeypatch, entity_id, effect_id):
    run = _dispatch(monkeypatch, "certified")
    monkeypatch.setattr(
        rec, "certify_od5a_physical_observation",
        lambda *args, **kwargs: SimpleNamespace(entity_id=entity_id, effect_id=effect_id),
    )
    with pytest.raises(RuntimeError, match="physical event identity"):
        run()


def test_unsupported_reconciliation_action_is_not_reclassified_as_progress(monkeypatch):
    run = _dispatch(monkeypatch, "not-a-phase")
    with pytest.raises(RuntimeError, match="unknown OD-5a reconciliation action"):
        run()
