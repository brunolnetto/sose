from __future__ import annotations

from types import SimpleNamespace

from sose.core.diagnostics import collect_runtime_diagnostics


class DiagnosticPersistence:
    def __init__(self, *, broken: bool) -> None:
        self.broken = broken
        self.position = object()
        self._known = "known"
        self._missing = "missing"

    def _ref(self) -> str:
        return self._missing if self.broken else self._known

    def scheduled_work(self):
        return [SimpleNamespace(work_id="work-1", command_id=self._ref())]

    def events(self):
        return [object()]

    def command(self, command_id):
        return object() if command_id == self._known else None

    def resource_definitions(self):
        return [SimpleNamespace(name=self._known)]

    def resource_demands(self):
        return [SimpleNamespace(request_id="rd-1", resource_name=self._ref())]

    def resource_reservations(self):
        return [SimpleNamespace(reservation_id="rr-1", resource_name=self._ref())]

    def resource_release_intents(self):
        return [SimpleNamespace(intent_id="ri-1", resource_name=self._ref())]

    def store_definitions(self):
        return [SimpleNamespace(name=self._known)]

    def store_items(self):
        return [SimpleNamespace(item_id="item-1", store_name=self._ref())]

    def store_put_intents(self):
        return [SimpleNamespace(item_id="put-1", store_name=self._ref())]

    def store_get_requests(self):
        return [SimpleNamespace(request_id="get-1", store_name=self._ref())]

    def store_get_results(self):
        return [SimpleNamespace(request_id="result-1", store_name=self._ref())]

    def container_definitions(self):
        return [SimpleNamespace(name=self._known)]

    def container_states(self):
        return [SimpleNamespace(name=self._ref())]

    def container_operation_intents(self):
        return [SimpleNamespace(request_id="ci-1", container_name=self._ref())]

    def container_operation_results(self):
        return [SimpleNamespace(request_id="cr-1", container_name=self._ref())]

    def preemptive_resource_definitions(self):
        return [SimpleNamespace(name=self._known)]

    def preemptive_resource_demands(self):
        return [SimpleNamespace(request_id="pd-1", resource_name=self._ref())]

    def preemptive_resource_reservations(self):
        return [SimpleNamespace(reservation_id="pr-1", resource_name=self._ref())]

    def preemptive_resource_release_intents(self):
        return [SimpleNamespace(intent_id="pi-1", resource_name=self._ref())]

    def resource_preemption_results(self):
        return [object()]

    def sink_deliveries(self):
        if self.broken:
            return [
                SimpleNamespace(
                    delivery_id="delivery-failed",
                    sink_name="warehouse",
                    status="pending",
                    last_error="boom",
                ),
                SimpleNamespace(
                    delivery_id="delivery-pending",
                    sink_name="warehouse",
                    status="pending",
                    last_error=None,
                ),
                SimpleNamespace(
                    delivery_id="delivery-done",
                    sink_name="warehouse",
                    status="delivered",
                    last_error="historic",
                ),
            ]
        return []

    def scenario_state(self):
        if self.broken:
            return SimpleNamespace(decisions=[1, 2], activations=[1])
        return None

    def simulation_position(self):
        return self.position


def test_runtime_diagnostics_reports_every_referential_failure_and_counts():
    diagnostics = collect_runtime_diagnostics(DiagnosticPersistence(broken=True))

    codes = [issue.code for issue in diagnostics.issues]
    assert codes.count("scheduled.command_missing") == 1
    assert codes.count("resource.definition_missing") == 3
    assert codes.count("store.definition_missing") == 4
    assert codes.count("container.definition_missing") == 3
    assert codes.count("preemptive.definition_missing") == 3
    assert codes.count("sink.delivery_failed") == 1
    assert diagnostics.healthy is False

    messages = "\n".join(issue.message for issue in diagnostics.issues)
    for identifier in (
        "work-1",
        "rd-1",
        "rr-1",
        "ri-1",
        "item-1",
        "put-1",
        "get-1",
        "result-1",
        "ci-1",
        "cr-1",
        "pd-1",
        "pr-1",
        "pi-1",
        "delivery-failed",
    ):
        assert identifier in messages

    counts = diagnostics.counts
    assert counts.events == 1
    assert counts.scheduled_work == 1
    assert counts.scenario_decisions == 2
    assert counts.scenario_activations == 1
    assert counts.resource_demands == 1
    assert counts.resource_reservations == 1
    assert counts.resource_release_intents == 1
    assert counts.store_items == 1
    assert counts.store_put_intents == 1
    assert counts.store_get_requests == 1
    assert counts.store_get_results == 1
    assert counts.container_operation_intents == 1
    assert counts.container_operation_results == 1
    assert counts.preemptive_resource_demands == 1
    assert counts.preemptive_resource_reservations == 1
    assert counts.preemptive_resource_release_intents == 1
    assert counts.resource_preemption_results == 1
    assert counts.sink_deliveries_pending == 2
    assert counts.sink_deliveries_failed == 1


def test_runtime_diagnostics_healthy_snapshot_exercises_valid_reference_paths():
    diagnostics = collect_runtime_diagnostics(DiagnosticPersistence(broken=False))

    assert diagnostics.healthy is True
    assert diagnostics.issues == ()
    assert diagnostics.counts.scenario_decisions == 0
    assert diagnostics.counts.scenario_activations == 0
    assert diagnostics.counts.sink_deliveries_pending == 0
    assert diagnostics.counts.sink_deliveries_failed == 0
