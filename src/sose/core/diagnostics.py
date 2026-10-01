from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from sose.core.runtime import SimulationPosition
from sose.persistence.base import Persistence


@dataclass(frozen=True, slots=True)
class DiagnosticIssue:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class RuntimeCounts:
    events: int
    scheduled_work: int
    scenario_decisions: int
    scenario_activations: int
    resource_demands: int
    resource_reservations: int
    resource_release_intents: int
    store_items: int
    store_put_intents: int
    store_get_requests: int
    store_get_results: int
    container_operation_intents: int
    container_operation_results: int
    preemptive_resource_demands: int
    preemptive_resource_reservations: int
    preemptive_resource_release_intents: int
    resource_preemption_results: int
    sink_deliveries_pending: int = 0
    sink_deliveries_failed: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeDiagnostics:
    position: SimulationPosition | None
    counts: RuntimeCounts
    issues: tuple[DiagnosticIssue, ...]

    @property
    def healthy(self) -> bool:
        return not self.issues


def _append_missing_reference_issues(
    issues: list[DiagnosticIssue],
    records: Iterable[Any],
    known_references: set[str],
    *,
    code: str,
    reference_of: Callable[[Any], str],
    message_of: Callable[[Any, str], str],
) -> None:
    """Append one issue for every record whose referenced definition is absent."""

    for record in records:
        reference = reference_of(record)
        if reference not in known_references:
            issues.append(DiagnosticIssue(code, message_of(record, reference)))


def collect_runtime_diagnostics(persistence: Persistence) -> RuntimeDiagnostics:
    """Inspect durable runtime truth without mutating or rebuilding it."""

    issues: list[DiagnosticIssue] = []

    # Snapshot every durable collection once. Besides making diagnostics cheaper
    # on remote stores, this ensures counts and referential checks describe the
    # same observation rather than independent reads.
    scheduled = persistence.scheduled_work()
    events = persistence.events()
    resource_definitions = persistence.resource_definitions()
    resource_demands = persistence.resource_demands()
    resource_reservations = persistence.resource_reservations()
    resource_release_intents = persistence.resource_release_intents()
    store_definitions = persistence.store_definitions()
    store_items = persistence.store_items()
    store_put_intents = persistence.store_put_intents()
    store_get_requests = persistence.store_get_requests()
    store_get_results = persistence.store_get_results()
    container_definitions = persistence.container_definitions()
    container_operation_intents = persistence.container_operation_intents()
    container_operation_results = persistence.container_operation_results()
    preemptive_definitions = persistence.preemptive_resource_definitions()
    preemptive_demands = persistence.preemptive_resource_demands()
    preemptive_reservations = persistence.preemptive_resource_reservations()
    preemptive_release_intents = persistence.preemptive_resource_release_intents()
    preemption_results = persistence.resource_preemption_results()
    sink_deliveries = persistence.sink_deliveries()

    for work in scheduled:
        if persistence.command(work.command_id) is None:
            issues.append(
                DiagnosticIssue(
                    "scheduled.command_missing",
                    f"scheduled work {work.work_id} references missing command "
                    f"{work.command_id}",
                )
            )

    resource_names = {definition.name for definition in resource_definitions}
    _append_missing_reference_issues(
        issues,
        resource_demands,
        resource_names,
        code="resource.definition_missing",
        reference_of=lambda demand: demand.resource_name,
        message_of=lambda demand, name: (
            f"resource demand {demand.request_id} references unknown resource {name}"
        ),
    )
    _append_missing_reference_issues(
        issues,
        resource_reservations,
        resource_names,
        code="resource.definition_missing",
        reference_of=lambda reservation: reservation.resource_name,
        message_of=lambda reservation, name: (
            f"resource reservation {reservation.reservation_id} references "
            f"unknown resource {name}"
        ),
    )
    _append_missing_reference_issues(
        issues,
        resource_release_intents,
        resource_names,
        code="resource.definition_missing",
        reference_of=lambda intent: intent.resource_name,
        message_of=lambda intent, name: (
            f"resource release intent {intent.intent_id} references unknown resource {name}"
        ),
    )

    store_names = {definition.name for definition in store_definitions}
    _append_missing_reference_issues(
        issues,
        store_items,
        store_names,
        code="store.definition_missing",
        reference_of=lambda item: item.store_name,
        message_of=lambda item, name: (
            f"store item {item.item_id} references unknown store {name}"
        ),
    )
    _append_missing_reference_issues(
        issues,
        store_put_intents,
        store_names,
        code="store.definition_missing",
        reference_of=lambda intent: intent.store_name,
        message_of=lambda intent, name: (
            f"store put intent {intent.item_id} references unknown store {name}"
        ),
    )
    _append_missing_reference_issues(
        issues,
        store_get_requests,
        store_names,
        code="store.definition_missing",
        reference_of=lambda request: request.store_name,
        message_of=lambda request, name: (
            f"store get request {request.request_id} references unknown store {name}"
        ),
    )
    _append_missing_reference_issues(
        issues,
        store_get_results,
        store_names,
        code="store.definition_missing",
        reference_of=lambda result: result.store_name,
        message_of=lambda result, name: (
            f"store get result {result.request_id} references unknown store {name}"
        ),
    )

    container_names = {definition.name for definition in container_definitions}
    container_states = persistence.container_states()
    _append_missing_reference_issues(
        issues,
        container_states,
        container_names,
        code="container.definition_missing",
        reference_of=lambda state: state.name,
        message_of=lambda state, name: f"container state {name} has no definition",
    )
    _append_missing_reference_issues(
        issues,
        container_operation_intents,
        container_names,
        code="container.definition_missing",
        reference_of=lambda intent: intent.container_name,
        message_of=lambda intent, name: (
            f"container intent {intent.request_id} references unknown container {name}"
        ),
    )
    _append_missing_reference_issues(
        issues,
        container_operation_results,
        container_names,
        code="container.definition_missing",
        reference_of=lambda result: result.container_name,
        message_of=lambda result, name: (
            f"container result {result.request_id} references unknown container {name}"
        ),
    )

    preemptive_names = {definition.name for definition in preemptive_definitions}
    _append_missing_reference_issues(
        issues,
        preemptive_demands,
        preemptive_names,
        code="preemptive.definition_missing",
        reference_of=lambda demand: demand.resource_name,
        message_of=lambda demand, name: (
            f"preemptive demand {demand.request_id} references unknown resource {name}"
        ),
    )
    _append_missing_reference_issues(
        issues,
        preemptive_reservations,
        preemptive_names,
        code="preemptive.definition_missing",
        reference_of=lambda reservation: reservation.resource_name,
        message_of=lambda reservation, name: (
            f"preemptive reservation {reservation.reservation_id} references "
            f"unknown resource {name}"
        ),
    )
    _append_missing_reference_issues(
        issues,
        preemptive_release_intents,
        preemptive_names,
        code="preemptive.definition_missing",
        reference_of=lambda intent: intent.resource_name,
        message_of=lambda intent, name: (
            f"preemptive release intent {intent.intent_id} references unknown resource {name}"
        ),
    )

    pending_sink_deliveries = tuple(
        delivery for delivery in sink_deliveries if delivery.status == "pending"
    )
    failed_sink_deliveries = tuple(
        delivery
        for delivery in pending_sink_deliveries
        if delivery.last_error is not None
    )
    issues.extend(
        DiagnosticIssue(
            "sink.delivery_failed",
            f"sink delivery {delivery.delivery_id} to "
            f"{delivery.sink_name!r} failed: {delivery.last_error}",
        )
        for delivery in failed_sink_deliveries
    )

    scenario_state = persistence.scenario_state()
    counts = RuntimeCounts(
        events=len(events),
        scheduled_work=len(scheduled),
        scenario_decisions=0 if scenario_state is None else len(scenario_state.decisions),
        scenario_activations=0 if scenario_state is None else len(scenario_state.activations),
        resource_demands=len(resource_demands),
        resource_reservations=len(resource_reservations),
        resource_release_intents=len(resource_release_intents),
        store_items=len(store_items),
        store_put_intents=len(store_put_intents),
        store_get_requests=len(store_get_requests),
        store_get_results=len(store_get_results),
        container_operation_intents=len(container_operation_intents),
        container_operation_results=len(container_operation_results),
        preemptive_resource_demands=len(preemptive_demands),
        preemptive_resource_reservations=len(preemptive_reservations),
        preemptive_resource_release_intents=len(preemptive_release_intents),
        resource_preemption_results=len(preemption_results),
        sink_deliveries_pending=len(pending_sink_deliveries),
        sink_deliveries_failed=len(failed_sink_deliveries),
    )
    return RuntimeDiagnostics(
        position=persistence.simulation_position(),
        counts=counts,
        issues=tuple(issues),
    )
