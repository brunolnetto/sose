from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from sose.examples.construction import scheduling as construction_scheduling
from sose.examples.construction import materials as construction_materials
from sose.examples.construction import simulation as construction_simulation
from sose.examples.hospitals import simulation as hospitals_simulation


class _FakeBackend:
    def __init__(self, now: datetime | None = None):
        self.now = now or datetime(2025, 1, 1)
        self.ran_until: datetime | None = None

    def run_until(self, due_at):
        self.ran_until = due_at


class _FakeCommands:
    def __init__(self):
        self.created = []

    def create(self, name, *, target, due_at, correlation_id, key):
        command = SimpleNamespace(
            id=f"cmd-{len(self.created) + 1}",
            name=name,
            target=target,
            due_at=due_at,
            correlation_id=correlation_id,
            key=key,
        )
        self.created.append(command)
        return command


class _FakeSchedules:
    def __init__(self):
        self.scheduled = []

    def at(self, due_at, *, command):
        self.scheduled.append((due_at, command))


class _FakeEngine:
    def __init__(self):
        self.context = SimpleNamespace(
            commands=_FakeCommands(),
            schedules=_FakeSchedules(),
        )

    def rebuild_backend(self, backend):
        self.backend = backend


def test_construction_planned_start_work_and_schedule_guards(monkeypatch):
    due_at = datetime(2025, 1, 1, 12, 0, 0)
    matching_work = SimpleNamespace(command_id="c1", due_at=due_at)
    other_work = SimpleNamespace(command_id="c2", due_at=due_at + timedelta(minutes=5))
    commands = {
        "c1": SimpleNamespace(
            entity_type="construction_activity",
            entity_id="A1",
            name="request_resources",
        ),
        "c2": SimpleNamespace(
            entity_type="construction_activity",
            entity_id="A2",
            name="request_resources",
        ),
    }

    class _SchedulingPersistence:
        def scheduled_work(self):
            return (other_work, matching_work)

        def command(self, command_id):
            return commands.get(command_id)

    found = construction_scheduling._planned_start_work(_SchedulingPersistence(), "A1")
    assert found is not None and found[0] is matching_work

    class _NoMatchPersistence:
        def scheduled_work(self):
            return (other_work,)

        def command(self, command_id):
            return None

    assert construction_scheduling._planned_start_work(_NoMatchPersistence(), "A1") is None

    ready_activity = SimpleNamespace(
        id="A1",
        state="ready",
        attributes={"material_staged": True},
    )
    waiting_activity = SimpleNamespace(
        id="A1",
        state="waiting_resource",
        attributes={"material_staged": True},
    )
    blocked_activity = SimpleNamespace(
        id="A1",
        state="queued",
        attributes={"material_staged": True},
    )
    unstaged_activity = SimpleNamespace(
        id="A1",
        state="ready",
        attributes={"material_staged": False},
    )
    entities = SimpleNamespace(activity_id="A1")
    backend = _FakeBackend()
    engine = _FakeEngine()

    monkeypatch.setattr(construction_scheduling, "activity", lambda *args, **kwargs: waiting_activity)
    assert (
        construction_scheduling.schedule_planned_start(
            object(),
            engine,
            backend,
            entities=entities,
        )
        == backend.now
    )

    monkeypatch.setattr(construction_scheduling, "activity", lambda *args, **kwargs: blocked_activity)
    with pytest.raises(RuntimeError, match="planned start requires Activity\\(ready\\)"):
        construction_scheduling.schedule_planned_start(
            object(),
            engine,
            backend,
            entities=entities,
        )

    monkeypatch.setattr(construction_scheduling, "activity", lambda *args, **kwargs: unstaged_activity)
    with pytest.raises(RuntimeError, match="material staging evidence"):
        construction_scheduling.schedule_planned_start(
            object(),
            engine,
            backend,
            entities=entities,
        )

    monkeypatch.setattr(construction_scheduling, "activity", lambda *args, **kwargs: ready_activity)
    monkeypatch.setattr(
        construction_scheduling,
        "_planned_start_work",
        lambda *args, **kwargs: (SimpleNamespace(due_at=due_at), object()),
    )
    assert (
        construction_scheduling.schedule_planned_start(
            object(),
            engine,
            backend,
            entities=entities,
        )
        == due_at
    )

    monkeypatch.setattr(construction_scheduling, "_planned_start_work", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        construction_scheduling,
        "flow_correlation_id",
        lambda activity_id: f"flow:{activity_id}",
    )
    created_due = construction_scheduling.schedule_planned_start(
        object(),
        engine,
        backend,
        entities=entities,
        delay=timedelta(minutes=15),
    )
    assert created_due == backend.now + timedelta(minutes=15)
    assert len(engine.context.schedules.scheduled) == 1


def test_construction_prepare_ready_guard_raises(monkeypatch):
    monkeypatch.setattr(
        construction_simulation,
        "seed_reference",
        lambda persistence, *, quantity: SimpleNamespace(activity_id="A1"),
    )
    monkeypatch.setattr(
        construction_simulation,
        "build_runtime",
        lambda persistence: (object(), _FakeEngine()),
    )
    monkeypatch.setattr(construction_simulation, "SimPyBackend", lambda origin: _FakeBackend())
    monkeypatch.setattr(construction_simulation, "reconcile_dependency", lambda *args, **kwargs: False)
    with pytest.raises(RuntimeError, match="dependency is not satisfied"):
        construction_simulation._prepare_ready_with_material(
            object(),
            quantity=1.0,
        )

    monkeypatch.setattr(construction_simulation, "reconcile_dependency", lambda *args, **kwargs: True)
    monkeypatch.setattr(construction_simulation, "seed_material", lambda *args, **kwargs: None)
    monkeypatch.setattr(construction_simulation, "reconcile_material_availability", lambda *args, **kwargs: None)
    monkeypatch.setattr(construction_simulation, "stage_material", lambda *args, **kwargs: False)
    with pytest.raises(RuntimeError, match="material was not staged"):
        construction_simulation._prepare_ready_with_material(
            object(),
            quantity=1.0,
        )


def test_construction_run_happy_path_error_branches(monkeypatch):
    entities = SimpleNamespace(activity_id="A1")
    engine = object()
    backend = _FakeBackend()
    monkeypatch.setattr(
        construction_simulation,
        "_prepare_ready_with_material",
        lambda persistence, *, quantity: (entities, engine, backend),
    )
    monkeypatch.setattr(
        construction_simulation,
        "schedule_planned_start",
        lambda *args, **kwargs: backend.now + timedelta(minutes=1),
    )
    monkeypatch.setattr(
        construction_simulation,
        "request_execution_resources",
        lambda *args, **kwargs: False,
    )
    with pytest.raises(RuntimeError, match="execution capacity unavailable"):
        construction_simulation.run_happy_path(quantity=1.0)

    monkeypatch.setattr(
        construction_simulation,
        "request_execution_resources",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(construction_simulation, "finish_execution", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        construction_simulation,
        "reconcile_inspection",
        lambda *args, **kwargs: False,
    )
    with pytest.raises(RuntimeError, match="inspection unavailable"):
        construction_simulation.run_happy_path(quantity=1.0)


def test_construction_run_rework_path_error_branches(monkeypatch):
    entities = SimpleNamespace(activity_id="A1")
    engine = object()
    backend = _FakeBackend()
    monkeypatch.setattr(
        construction_simulation,
        "_prepare_ready_with_material",
        lambda persistence, *, quantity: (entities, engine, backend),
    )
    monkeypatch.setattr(
        construction_simulation,
        "schedule_planned_start",
        lambda *args, **kwargs: backend.now + timedelta(minutes=1),
    )
    monkeypatch.setattr(
        construction_simulation,
        "request_execution_resources",
        lambda *args, **kwargs: False,
    )
    with pytest.raises(RuntimeError, match="execution capacity unavailable"):
        construction_simulation.run_rework_path(quantity=1.0)

    request_outcomes = iter([True, True])
    monkeypatch.setattr(
        construction_simulation,
        "request_execution_resources",
        lambda *args, **kwargs: next(request_outcomes),
    )
    monkeypatch.setattr(construction_simulation, "finish_execution", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        construction_simulation,
        "reconcile_inspection",
        lambda *args, **kwargs: False,
    )
    with pytest.raises(RuntimeError, match="first construction inspection unavailable"):
        construction_simulation.run_rework_path(quantity=1.0)

    request_outcomes = iter([True, False])
    inspect_outcomes = iter([True])
    monkeypatch.setattr(
        construction_simulation,
        "request_execution_resources",
        lambda *args, **kwargs: next(request_outcomes),
    )
    monkeypatch.setattr(
        construction_simulation,
        "reconcile_inspection",
        lambda *args, **kwargs: next(inspect_outcomes),
    )
    with pytest.raises(RuntimeError, match="rework capacity unavailable"):
        construction_simulation.run_rework_path(quantity=1.0)

    request_outcomes = iter([True, True])
    inspect_outcomes = iter([True, False])
    monkeypatch.setattr(
        construction_simulation,
        "request_execution_resources",
        lambda *args, **kwargs: next(request_outcomes),
    )
    monkeypatch.setattr(
        construction_simulation,
        "reconcile_inspection",
        lambda *args, **kwargs: next(inspect_outcomes),
    )
    with pytest.raises(RuntimeError, match="reinspection unavailable"):
        construction_simulation.run_rework_path(quantity=1.0)


def _patch_hospital_runtime(monkeypatch):
    entities = SimpleNamespace(admission_id="ADM1", treatment_episode_id="EP1")
    monkeypatch.setattr(hospitals_simulation, "seed_reference", lambda *args, **kwargs: entities)
    monkeypatch.setattr(hospitals_simulation, "build_runtime", lambda persistence: (object(), _FakeEngine()))
    monkeypatch.setattr(hospitals_simulation, "SimPyBackend", lambda origin: _FakeBackend())
    monkeypatch.setattr(hospitals_simulation, "triage_and_queue", lambda *args, **kwargs: None)
    monkeypatch.setattr(hospitals_simulation, "deteriorate_to_icu_wait", lambda *args, **kwargs: None)
    monkeypatch.setattr(hospitals_simulation, "discharge_from_icu", lambda *args, **kwargs: None)
    monkeypatch.setattr(hospitals_simulation, "discharge_from_ward", lambda *args, **kwargs: None)
    monkeypatch.setattr(hospitals_simulation, "complete_procedure", lambda *args, **kwargs: None)
    return entities


def test_hospitals_happy_and_icu_guard_errors(monkeypatch):
    entities = _patch_hospital_runtime(monkeypatch)

    monkeypatch.setattr(hospitals_simulation, "claim_next_ward_admission", lambda *args, **kwargs: "OTHER")
    with pytest.raises(RuntimeError, match="reference admission was not allocated"):
        hospitals_simulation.run_happy_path()

    with pytest.raises(RuntimeError, match="reference admission was not allocated"):
        hospitals_simulation.run_icu_path()

    monkeypatch.setattr(hospitals_simulation, "claim_next_ward_admission", lambda *args, **kwargs: entities.admission_id)
    monkeypatch.setattr(hospitals_simulation, "reconcile_icu_allocation", lambda *args, **kwargs: False)
    with pytest.raises(RuntimeError, match="ICU capacity unavailable"):
        hospitals_simulation.run_icu_path()


def test_hospitals_preemption_guard_errors(monkeypatch):
    entities = _patch_hospital_runtime(monkeypatch)
    monkeypatch.setattr(hospitals_simulation, "claim_next_ward_admission", lambda *args, **kwargs: "OTHER")
    with pytest.raises(RuntimeError, match="reference admission was not allocated"):
        hospitals_simulation.run_preemption_path()

    monkeypatch.setattr(hospitals_simulation, "claim_next_ward_admission", lambda *args, **kwargs: entities.admission_id)
    monkeypatch.setattr(hospitals_simulation, "reconcile_procedure_start", lambda *args, **kwargs: False)
    with pytest.raises(RuntimeError, match="procedure capacity unavailable"):
        hospitals_simulation.run_preemption_path()

    monkeypatch.setattr(hospitals_simulation, "reconcile_procedure_start", lambda *args, **kwargs: True)
    monkeypatch.setattr(hospitals_simulation, "commit_emergency_preemption", lambda *args, **kwargs: False)
    with pytest.raises(RuntimeError, match="preemption was not committed"):
        hospitals_simulation.run_preemption_path()

    monkeypatch.setattr(hospitals_simulation, "commit_emergency_preemption", lambda *args, **kwargs: True)
    monkeypatch.setattr(hospitals_simulation, "reconcile_preemption_business", lambda *args, **kwargs: False)
    with pytest.raises(RuntimeError, match="preemption did not reach business state"):
        hospitals_simulation.run_preemption_path()

    monkeypatch.setattr(hospitals_simulation, "reconcile_preemption_business", lambda *args, **kwargs: True)
    monkeypatch.setattr(hospitals_simulation, "complete_emergency_and_resume", lambda *args, **kwargs: False)
    with pytest.raises(RuntimeError, match="elective procedure did not resume"):
        hospitals_simulation.run_preemption_path()


def test_construction_materials_guard_and_helper_edges(monkeypatch):
    with pytest.raises(ValueError, match="quantity must be positive"):
        construction_materials.seed_material(
            object(),
            object(),
            SimpleNamespace(now=datetime(2025, 1, 1), run_until=lambda *_args: None),
            quantity=0.0,
        )

    staged_activity = SimpleNamespace(attributes={"material_staged": True})
    monkeypatch.setattr(construction_materials, "activity", lambda *_args, **_kwargs: staged_activity)
    assert construction_materials.material_feasible(
        SimpleNamespace(store_items=lambda: (), container_states=lambda: ()),
        SimpleNamespace(context=SimpleNamespace(scenarios=SimpleNamespace(attribute=lambda *_args: True))),
        entities=SimpleNamespace(activity_id="A1"),
    ) is True

    unstaged_activity = SimpleNamespace(attributes={"material_staged": False, "quantity": 5.0})
    monkeypatch.setattr(construction_materials, "activity", lambda *_args, **_kwargs: unstaged_activity)
    assert construction_materials.material_feasible(
        SimpleNamespace(store_items=lambda: (), container_states=lambda: ()),
        SimpleNamespace(context=SimpleNamespace(scenarios=SimpleNamespace(attribute=lambda *_args: False))),
        entities=SimpleNamespace(activity_id="A1"),
    ) is False

    reconciled: list[bool] = []
    monkeypatch.setattr(construction_materials, "material_feasible", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        construction_materials,
        "reconcile_material_availability",
        lambda *_args, **_kwargs: reconciled.append(True),
    )
    assert construction_materials._prerequisites_or_reconcile(
        object(),
        object(),
        entities=SimpleNamespace(activity_id="A1"),
        lot_result=None,
        quantity_done=False,
    ) is False
    assert reconciled == [True]


def test_construction_materials_ensure_staging_and_stage_material_edges(monkeypatch):
    class _Persistence:
        def store_get_results(self):
            return ()

        def store_get_requests(self):
            return ()

        def container_operation_results(self):
            return (SimpleNamespace(request_id="stage-quantity:A1"),)

        def container_operation_intents(self):
            return ()

    calls: list[tuple[str, str]] = []
    engine = SimpleNamespace(
        stores=SimpleNamespace(
            get=lambda backend, *, store_name, request_id, requested_at: calls.append(("store", request_id))
        ),
        containers=SimpleNamespace(
            get=lambda backend, *, container_name, request_id, amount, requested_at: calls.append(("container", request_id))
        ),
    )
    backend = SimpleNamespace(now=datetime(2025, 1, 1), run_until=lambda *_args: None)
    stage_persistence = _Persistence()
    construction_materials._ensure_staging_requests(
        stage_persistence,
        engine,
        backend,
        lot_request="stage-lot:A1",
        qty_request="stage-quantity:A1",
        quantity=5.0,
    )
    assert calls == [("store", "stage-lot:A1")]

    monkeypatch.setattr(
        construction_materials,
        "activity",
        lambda *_args, **_kwargs: SimpleNamespace(
            id="A1",
            state="ready",
            attributes={"material_staged": True, "quantity": 5.0},
        ),
    )
    assert construction_materials.stage_material(
        stage_persistence,
        object(),
        backend,
        entities=SimpleNamespace(activity_id="A1"),
    ) is True

    monkeypatch.setattr(
        construction_materials,
        "activity",
        lambda *_args, **_kwargs: SimpleNamespace(
            id="A1",
            state="planned",
            attributes={"material_staged": False, "quantity": 5.0},
        ),
    )
    assert construction_materials.stage_material(
        stage_persistence,
        object(),
        backend,
        entities=SimpleNamespace(activity_id="A1"),
    ) is False

    monkeypatch.setattr(
        construction_materials,
        "activity",
        lambda *_args, **_kwargs: SimpleNamespace(
            id="A1",
            state="ready",
            attributes={"material_staged": False, "quantity": 5.0},
        ),
    )
    monkeypatch.setattr(construction_materials, "_prerequisites_or_reconcile", lambda *_args, **_kwargs: False)
    assert construction_materials.stage_material(
        stage_persistence,
        object(),
        backend,
        entities=SimpleNamespace(activity_id="A1"),
    ) is False

    monkeypatch.setattr(construction_materials, "_prerequisites_or_reconcile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(construction_materials, "_ensure_staging_requests", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(construction_materials, "_store_get_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(construction_materials, "_has_container_result", lambda *_args, **_kwargs: False)
    assert construction_materials.stage_material(
        stage_persistence,
        object(),
        backend,
        entities=SimpleNamespace(activity_id="A1"),
    ) is False
