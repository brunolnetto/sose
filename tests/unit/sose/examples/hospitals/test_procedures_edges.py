from __future__ import annotations

from types import SimpleNamespace

from sose.examples.hospitals import procedures as procedures_module
from sose.examples.hospitals.procedures import (
    complete_emergency_and_resume,
    reconcile_procedure_start,
    reconcile_scenario_emergency,
)


def _engine_stub(
    *,
    ensure_requested=lambda *args, **kwargs: object(),
    reservation_for=lambda *args, **kwargs: None,
    withdraw=lambda *args, **kwargs: None,
    emergency_active=False,
):
    return SimpleNamespace(
        preemptive_resources=SimpleNamespace(
            ensure_requested=ensure_requested,
            reservation_for=reservation_for,
            withdraw=withdraw,
        ),
        context=SimpleNamespace(
            scenarios=SimpleNamespace(
                attribute=lambda name, default=False: emergency_active
                if name == "hospital.procedure.emergency"
                else default
            )
        ),
    )


def test_reconcile_procedure_start_returns_true_without_start_dispatch_when_state_changes_after_reservation(
    monkeypatch,
):
    waiting = SimpleNamespace(
        id="E1",
        state="waiting_capacity",
        attributes={"admission_id": "A1"},
    )
    progressed = SimpleNamespace(
        id="E1",
        state="in_progress",
        attributes={"admission_id": "A1"},
    )
    states = iter([waiting, progressed])
    monkeypatch.setattr(procedures_module, "episode", lambda *args, **kwargs: next(states))
    monkeypatch.setattr(
        procedures_module,
        "admission",
        lambda *args, **kwargs: SimpleNamespace(state="treatment"),
    )
    events: list[str] = []
    monkeypatch.setattr(
        procedures_module,
        "dispatch",
        lambda _engine, _entity, event, **kwargs: events.append(event),
    )
    engine = _engine_stub(reservation_for=lambda *args, **kwargs: object())
    backend = SimpleNamespace(now="N/A")

    assert reconcile_procedure_start(
        object(),
        engine,
        backend,
        episode_id="E1",
    )
    assert events == []


def test_complete_emergency_and_resume_returns_false_when_normal_not_interrupted_and_no_preemption(
    monkeypatch,
):
    monkeypatch.setattr(
        procedures_module,
        "reconcile_preemption_business",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        procedures_module,
        "episode",
        lambda _persistence, _episode_id: SimpleNamespace(state="waiting_capacity"),
    )

    assert (
        complete_emergency_and_resume(
            object(),
            _engine_stub(),
            object(),
            normal_episode_id="N1",
        )
        is False
    )


def test_complete_emergency_and_resume_dispatches_emergency_start_and_returns_false_when_not_completed(
    monkeypatch,
):
    normal_id = "N2"
    emergency_id = procedures_module.emergency_episode_id(normal_id)
    normal = SimpleNamespace(
        id=normal_id,
        state="interrupted",
        attributes={"admission_id": "A2"},
    )
    emergency = SimpleNamespace(
        id=emergency_id,
        state="waiting_capacity",
        attributes={"admission_id": "A2"},
    )

    monkeypatch.setattr(
        procedures_module,
        "reconcile_preemption_business",
        lambda *args, **kwargs: False,
    )

    def _episode(_persistence, episode_id):
        return normal if episode_id == normal_id else emergency

    monkeypatch.setattr(procedures_module, "episode", _episode)
    events: list[str] = []
    monkeypatch.setattr(
        procedures_module,
        "dispatch",
        lambda _engine, _entity, event, **kwargs: events.append(event),
    )
    engine = _engine_stub(
        reservation_for=lambda request_id: object()
        if request_id == procedures_module.emergency_request_id(normal_id)
        else None
    )

    assert (
        complete_emergency_and_resume(
            object(),
            engine,
            object(),
            normal_episode_id=normal_id,
        )
        is False
    )
    assert events == ["start"]


def test_complete_emergency_and_resume_returns_false_when_normal_capacity_cannot_be_reclaimed(
    monkeypatch,
):
    normal_id = "N3"
    emergency = SimpleNamespace(
        id=procedures_module.emergency_episode_id(normal_id),
        state="completed",
        attributes={"admission_id": "A3"},
    )
    normal = SimpleNamespace(
        id=normal_id,
        state="interrupted",
        attributes={"admission_id": "A3"},
    )
    monkeypatch.setattr(
        procedures_module,
        "reconcile_preemption_business",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        procedures_module,
        "episode",
        lambda _persistence, episode_id: normal if episode_id == normal_id else emergency,
    )

    assert (
        complete_emergency_and_resume(
            object(),
            _engine_stub(ensure_requested=lambda *args, **kwargs: None),
            SimpleNamespace(now="N/A"),
            normal_episode_id=normal_id,
        )
        is False
    )


def test_complete_emergency_and_resume_skips_resume_when_normal_is_not_interrupted(
    monkeypatch,
):
    normal_id = "N4"
    emergency = SimpleNamespace(
        id=procedures_module.emergency_episode_id(normal_id),
        state="completed",
        attributes={"admission_id": "A4"},
    )
    normal = SimpleNamespace(
        id=normal_id,
        state="planned",
        attributes={"admission_id": "A4"},
    )
    monkeypatch.setattr(
        procedures_module,
        "reconcile_preemption_business",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        procedures_module,
        "episode",
        lambda _persistence, episode_id: normal if episode_id == normal_id else emergency,
    )
    events: list[str] = []
    monkeypatch.setattr(
        procedures_module,
        "dispatch",
        lambda _engine, _entity, event, **kwargs: events.append(event),
    )

    assert (
        complete_emergency_and_resume(
            object(),
            _engine_stub(),
            SimpleNamespace(now="N/A"),
            normal_episode_id=normal_id,
        )
        is False
    )
    assert events == []


def test_reconcile_scenario_emergency_reconciles_business_after_successful_commit(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        procedures_module,
        "commit_emergency_preemption",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        procedures_module,
        "reconcile_preemption_business",
        lambda *args, **kwargs: calls.append("reconciled") or True,
    )
    monkeypatch.setattr(procedures_module, "maybe_episode", lambda *args, **kwargs: None)

    assert reconcile_scenario_emergency(
        object(),
        _engine_stub(emergency_active=True),
        object(),
        normal_episode_id="N5",
    )
    assert calls == ["reconciled"]


def test_reconcile_scenario_emergency_returns_false_when_commit_does_not_happen(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        procedures_module,
        "commit_emergency_preemption",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        procedures_module,
        "reconcile_preemption_business",
        lambda *args, **kwargs: calls.append("reconciled") or True,
    )
    monkeypatch.setattr(procedures_module, "maybe_episode", lambda *args, **kwargs: None)

    assert (
        reconcile_scenario_emergency(
            object(),
            _engine_stub(emergency_active=True),
            object(),
            normal_episode_id="N6",
        )
        is False
    )
    assert calls == []
