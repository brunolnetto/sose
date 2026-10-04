from datetime import timedelta
from types import SimpleNamespace

from sose.backends.simpy import SimPyBackend
from sose.core.diagnostics import collect_runtime_diagnostics
from sose.core.runtime import (
    ResourceDemand,
    ScheduledWork,
    StoreGetRequest,
)
from sose.examples.tutorial_job.simulation import (
    ORIGIN,
    build_runtime,
    seed_job,
    start_and_schedule_completion,
)
from sose.persistence.memory import MemoryPersistence


def test_engine_diagnostics_reports_clean_pending_runtime():
    persistence = MemoryPersistence()
    job = seed_job(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    start_and_schedule_completion(
        persistence,
        engine,
        job_id=job.id,
        complete_at=ORIGIN + timedelta(hours=2),
    )

    diagnostics = engine.diagnostics()

    assert diagnostics.healthy
    assert diagnostics.issues == ()
    assert diagnostics.counts.events == 1
    assert diagnostics.counts.scheduled_work == 1
    assert diagnostics.counts.resource_demands == 0
    assert diagnostics.counts.store_get_requests == 0


def test_diagnostics_follow_runtime_consumption_without_mutation():
    persistence = MemoryPersistence()
    job = seed_job(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    complete_at = ORIGIN + timedelta(hours=1)

    start_and_schedule_completion(
        persistence,
        engine,
        job_id=job.id,
        complete_at=complete_at,
    )
    before = engine.diagnostics()
    backend.run_until(complete_at)
    after = engine.diagnostics()

    assert before.counts.scheduled_work == 1
    assert after.counts.scheduled_work == 0
    assert after.counts.events == 2
    assert after.healthy


def test_corruption_is_reported_with_stable_issue_codes():
    persistence = MemoryPersistence()

    persistence._state.scheduled_work["orphan-work"] = ScheduledWork(
        "orphan-work",
        ORIGIN + timedelta(hours=1),
        priority=100,
        sequence=1,
        command_id="missing-command",
    )
    persistence._state.resource_demands["orphan-demand"] = ResourceDemand(
        request_id="orphan-demand",
        resource_name="missing-resource",
        priority=100,
        requested_at=ORIGIN,
        sequence=1,
    )
    persistence._state.store_get_requests["orphan-get"] = StoreGetRequest(
        request_id="orphan-get",
        store_name="missing-store",
        requested_at=ORIGIN,
        sequence=1,
    )

    before = persistence._state
    diagnostics = collect_runtime_diagnostics(persistence)

    assert not diagnostics.healthy
    assert [issue.code for issue in diagnostics.issues] == [
        "scheduled.command_missing",
        "resource.definition_missing",
        "store.definition_missing",
    ]
    assert persistence._state is before


def test_diagnostics_are_deterministic_for_same_durable_truth():
    persistence = MemoryPersistence()
    job = seed_job(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    start_and_schedule_completion(
        persistence,
        engine,
        job_id=job.id,
        complete_at=ORIGIN + timedelta(hours=3),
    )

    assert engine.diagnostics() == engine.diagnostics()


def test_diagnostics_reports_failed_sink_delivery():
    from sose.jobs.model import SimulationJobState
    from sose.sinks.model import AnalyticalBatch, SinkDelivery
    from sose.core.events import DomainEvent

    persistence = MemoryPersistence()
    event = DomainEvent(
        event_id="sink-event",
        name="changed",
        entity_type="demo",
        entity_id="1",
        occurred_at=ORIGIN,
        tick=1,
    )
    batch = AnalyticalBatch(
        batch_id="batch-failed",
        job_id="job-1",
        domain_name="demo",
        config_revision=1,
        logical_tick=1,
        logical_time=ORIGIN,
        from_event_offset=0,
        to_event_offset=1,
        events=(event,),
    )
    with persistence.transaction() as uow:
        uow.save_job_state(
            SimulationJobState(
                job_id="job-1",
                domain_name="demo",
                config_json='{"start_at":"2026-01-01T00:00:00Z"}',
                config_revision=1,
                status="ready",
                initialized=True,
                logical_time=ORIGIN,
                next_tick=1,
            )
        )
        uow.save_sink_delivery(
            SinkDelivery(
                delivery_id="delivery-failed",
                sink_name="warehouse",
                batch=batch,
                attempts=1,
                last_error="RuntimeError: unavailable",
            )
        )

    diagnostics = collect_runtime_diagnostics(persistence)

    assert not diagnostics.healthy
    assert diagnostics.counts.sink_deliveries_pending == 1
    assert diagnostics.counts.sink_deliveries_failed == 1
    assert [issue.code for issue in diagnostics.issues] == [
        "sink.delivery_failed",
    ]



def _record(**fields):
    return SimpleNamespace(**fields)


class _DiagnosticsFixturePersistence:
    def __init__(self):
        self.calls = {}
        self.commands = {"command-ok": object()}
        self.values = {
            "scheduled_work": (
                _record(work_id="work-ok", command_id="command-ok"),
                _record(work_id="work-bad", command_id="command-missing"),
            ),
            "events": (object(), object()),
            "resource_definitions": (_record(name="resource-ok"),),
            "resource_demands": (
                _record(request_id="d-ok", resource_name="resource-ok"),
                _record(request_id="d-bad", resource_name="resource-missing"),
            ),
            "resource_reservations": (
                _record(reservation_id="r-ok", resource_name="resource-ok"),
                _record(reservation_id="r-bad", resource_name="resource-missing"),
            ),
            "resource_release_intents": (
                _record(intent_id="rel-ok", resource_name="resource-ok"),
                _record(intent_id="rel-bad", resource_name="resource-missing"),
            ),
            "store_definitions": (_record(name="store-ok"),),
            "store_items": (
                _record(item_id="item-ok", store_name="store-ok"),
                _record(item_id="item-bad", store_name="store-missing"),
            ),
            "store_put_intents": (
                _record(item_id="put-ok", store_name="store-ok"),
                _record(item_id="put-bad", store_name="store-missing"),
            ),
            "store_get_requests": (
                _record(request_id="get-ok", store_name="store-ok"),
                _record(request_id="get-bad", store_name="store-missing"),
            ),
            "store_get_results": (
                _record(request_id="result-ok", store_name="store-ok"),
                _record(request_id="result-bad", store_name="store-missing"),
            ),
            "container_definitions": (_record(name="container-ok"),),
            "container_states": (
                _record(name="container-ok"),
                _record(name="container-missing"),
            ),
            "container_operation_intents": (
                _record(request_id="ci-ok", container_name="container-ok"),
                _record(request_id="ci-bad", container_name="container-missing"),
            ),
            "container_operation_results": (
                _record(request_id="cr-ok", container_name="container-ok"),
                _record(request_id="cr-bad", container_name="container-missing"),
            ),
            "preemptive_resource_definitions": (_record(name="preemptive-ok"),),
            "preemptive_resource_demands": (
                _record(request_id="pd-ok", resource_name="preemptive-ok"),
                _record(request_id="pd-bad", resource_name="preemptive-missing"),
            ),
            "preemptive_resource_reservations": (
                _record(reservation_id="pr-ok", resource_name="preemptive-ok"),
                _record(reservation_id="pr-bad", resource_name="preemptive-missing"),
            ),
            "preemptive_resource_release_intents": (
                _record(intent_id="pi-ok", resource_name="preemptive-ok"),
                _record(intent_id="pi-bad", resource_name="preemptive-missing"),
            ),
            "resource_preemption_results": (object(), object()),
            "sink_deliveries": (
                _record(
                    delivery_id="sink-failed",
                    sink_name="warehouse",
                    status="pending",
                    last_error="boom",
                ),
                _record(
                    delivery_id="sink-pending",
                    sink_name="warehouse",
                    status="pending",
                    last_error=None,
                ),
                _record(
                    delivery_id="sink-complete",
                    sink_name="warehouse",
                    status="delivered",
                    last_error="ignored",
                ),
            ),
        }
        self._scenario_state = _record(decisions=(1, 2), activations=(1,))
        self._position = _record(logical_tick=3)

    def _read(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1
        return self.values[name]

    def command(self, command_id):
        self.calls["command"] = self.calls.get("command", 0) + 1
        return self.commands.get(command_id)

    def scenario_state(self):
        self.calls["scenario_state"] = self.calls.get("scenario_state", 0) + 1
        return self._scenario_state

    def simulation_position(self):
        self.calls["simulation_position"] = self.calls.get("simulation_position", 0) + 1
        return self._position


for _name in (
    "scheduled_work",
    "events",
    "resource_definitions",
    "resource_demands",
    "resource_reservations",
    "resource_release_intents",
    "store_definitions",
    "store_items",
    "store_put_intents",
    "store_get_requests",
    "store_get_results",
    "container_definitions",
    "container_states",
    "container_operation_intents",
    "container_operation_results",
    "preemptive_resource_definitions",
    "preemptive_resource_demands",
    "preemptive_resource_reservations",
    "preemptive_resource_release_intents",
    "resource_preemption_results",
    "sink_deliveries",
):
    setattr(
        _DiagnosticsFixturePersistence,
        _name,
        lambda self, name=_name: self._read(name),
    )


def test_diagnostics_reference_matrix_and_snapshot_reads_are_complete():
    persistence = _DiagnosticsFixturePersistence()

    diagnostics = collect_runtime_diagnostics(persistence)

    assert [issue.code for issue in diagnostics.issues] == [
        "scheduled.command_missing",
        "resource.definition_missing",
        "resource.definition_missing",
        "resource.definition_missing",
        "store.definition_missing",
        "store.definition_missing",
        "store.definition_missing",
        "store.definition_missing",
        "container.definition_missing",
        "container.definition_missing",
        "container.definition_missing",
        "preemptive.definition_missing",
        "preemptive.definition_missing",
        "preemptive.definition_missing",
        "sink.delivery_failed",
    ]
    assert diagnostics.issues[8].message == (
        "container state container-missing has no definition"
    )
    assert diagnostics.counts.events == 2
    assert diagnostics.counts.scenario_decisions == 2
    assert diagnostics.counts.scenario_activations == 1
    assert diagnostics.counts.resource_preemption_results == 2
    assert diagnostics.counts.sink_deliveries_pending == 2
    assert diagnostics.counts.sink_deliveries_failed == 1
    assert diagnostics.position.logical_tick == 3

    for name in persistence.values:
        assert persistence.calls[name] == 1
    assert persistence.calls["command"] == 2
    assert persistence.calls["scenario_state"] == 1
    assert persistence.calls["simulation_position"] == 1
