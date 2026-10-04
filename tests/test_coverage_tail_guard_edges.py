from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from sose.core.runtime import _require_nonnegative_finite
from sose.examples.canonical import common as canonical_common
from sose.examples.canonical import job_shop
from sose.examples.cards_payments import definition as cards_definition
from sose.examples.construction import runtime as construction_runtime
from sose.examples.hospitals import runtime as hospitals_runtime
from sose.examples.logistics import definition as logistics_definition
from sose.persistence.memory import MemoryPersistence


def test_nonnegative_runtime_guard_rejects_negative_and_non_finite_values():
    with pytest.raises(ValueError, match="must be finite and >= 0"):
        _require_nonnegative_finite(float("inf"), label="level")
    with pytest.raises(ValueError, match="must be finite and >= 0"):
        _require_nonnegative_finite(-1.0, label="level")


def test_cards_reconcile_tick_raises_when_seed_payment_is_missing():
    persistence = SimpleNamespace(entity=lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="configured payment was not persisted"):
        cards_definition._reconcile_tick(
            persistence,
            object(),
            object(),
            cards_definition.definition.default_config(),
            SimpleNamespace(payment_id="payment-1"),
        )


def test_logistics_shipment_or_error_raises_when_shipment_is_missing():
    persistence = SimpleNamespace(entity=lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="configured shipment was not persisted"):
        logistics_definition._shipment_or_error(
            persistence,
            SimpleNamespace(shipment_id="shipment-1"),
        )


def test_construction_runtime_guards_reject_invalid_inputs():
    with pytest.raises(ValueError, match="inspection ordinal must be >= 1"):
        construction_runtime.inspection_id("activity-1", 0)
    with pytest.raises(ValueError, match="quantity must fit construction material capacity"):
        construction_runtime.seed_reference(MemoryPersistence(), quantity=0.0)
    with pytest.raises(RuntimeError, match="construction activity was not persisted"):
        construction_runtime.activity(MemoryPersistence(), "missing-activity")


def test_hospitals_runtime_lookup_helpers_raise_when_entities_are_missing():
    persistence = MemoryPersistence()

    with pytest.raises(RuntimeError, match="admission was not persisted"):
        hospitals_runtime.admission(persistence, "missing-admission")
    with pytest.raises(RuntimeError, match="treatment episode was not persisted"):
        hospitals_runtime.episode(persistence, "missing-episode")


def test_canonical_action_resolution_reuses_pending_put_intent():
    pending = SimpleNamespace(
        store_name=canonical_common.ACTION_STORE,
        item_id="job_shop:tick:7",
        value={"action": "active:finish"},
    )
    persistence = SimpleNamespace(
        store_items=lambda: (),
        store_put_intents=lambda: (pending,),
    )

    action = canonical_common.resolve_tick_action(
        persistence,
        object(),
        canonical="job_shop",
        logical_tick=7,
        candidate="ready",
        requested_at=datetime.now(timezone.utc),
    )

    assert action == "active:finish"


def test_job_shop_reconcile_ignores_active_action_when_case_is_not_active(monkeypatch):
    current = SimpleNamespace(id="case-1", state="ready", attributes={})
    persistence = SimpleNamespace(
        entity=lambda *_args, **_kwargs: current,
    )
    engine = SimpleNamespace(
        resources=SimpleNamespace(),
        context=SimpleNamespace(
            clock=SimpleNamespace(now=datetime.now(timezone.utc), tick=3),
        ),
    )
    monkeypatch.setattr(job_shop, "_candidate_state", lambda **_kwargs: "ready")
    monkeypatch.setattr(
        job_shop,
        "resolve_tick_action",
        lambda *_args, **_kwargs: "active:job-0-op-1",
    )
    monkeypatch.setattr(
        job_shop,
        "transition",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("transition should not run for a non-active case")
        ),
    )

    job_shop.reconcile(
        persistence,
        engine,
        object(),
        job_shop.definition.default_config(),
        SimpleNamespace(id=current.id),
    )
