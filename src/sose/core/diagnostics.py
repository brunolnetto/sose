from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class RuntimeDiagnostics:
    position: SimulationPosition | None
    counts: RuntimeCounts
    issues: tuple[DiagnosticIssue, ...]

    @property
    def healthy(self) -> bool:
        return not self.issues


def collect_runtime_diagnostics(persistence: Persistence) -> RuntimeDiagnostics:
    """Inspect durable runtime truth without mutating or rebuilding it."""

    issues: list[DiagnosticIssue] = []

    scheduled = persistence.scheduled_work()
    for work in scheduled:
        if persistence.command(work.command_id) is None:
            issues.append(
                DiagnosticIssue(
                    "scheduled.command_missing",
                    f"scheduled work {work.work_id} references missing command "
                    f"{work.command_id}",
                )
            )

    resource_definitions = {
        definition.name for definition in persistence.resource_definitions()
    }
    for demand in persistence.resource_demands():
        if demand.resource_name not in resource_definitions:
            issues.append(
                DiagnosticIssue(
                    "resource.definition_missing",
                    f"resource demand {demand.request_id} references unknown "
                    f"resource {demand.resource_name}",
                )
            )
    for reservation in persistence.resource_reservations():
        if reservation.resource_name not in resource_definitions:
            issues.append(
                DiagnosticIssue(
                    "resource.definition_missing",
                    f"resource reservation {reservation.reservation_id} references "
                    f"unknown resource {reservation.resource_name}",
                )
            )
    for intent in persistence.resource_release_intents():
        if intent.resource_name not in resource_definitions:
            issues.append(
                DiagnosticIssue(
                    "resource.definition_missing",
                    f"resource release intent {intent.intent_id} references unknown "
                    f"resource {intent.resource_name}",
                )
            )

    store_definitions = {
        definition.name for definition in persistence.store_definitions()
    }
    for item in persistence.store_items():
        if item.store_name not in store_definitions:
            issues.append(
                DiagnosticIssue(
                    "store.definition_missing",
                    f"store item {item.item_id} references unknown store "
                    f"{item.store_name}",
                )
            )
    for intent in persistence.store_put_intents():
        if intent.store_name not in store_definitions:
            issues.append(
                DiagnosticIssue(
                    "store.definition_missing",
                    f"store put intent {intent.item_id} references unknown store "
                    f"{intent.store_name}",
                )
            )
    for request in persistence.store_get_requests():
        if request.store_name not in store_definitions:
            issues.append(
                DiagnosticIssue(
                    "store.definition_missing",
                    f"store get request {request.request_id} references unknown store "
                    f"{request.store_name}",
                )
            )
    for result in persistence.store_get_results():
        if result.store_name not in store_definitions:
            issues.append(
                DiagnosticIssue(
                    "store.definition_missing",
                    f"store get result {result.request_id} references unknown store "
                    f"{result.store_name}",
                )
            )

    container_definitions = {
        definition.name for definition in persistence.container_definitions()
    }
    for state in persistence.container_states():
        if state.name not in container_definitions:
            issues.append(
                DiagnosticIssue(
                    "container.definition_missing",
                    f"container state {state.name} has no definition",
                )
            )
    for intent in persistence.container_operation_intents():
        if intent.container_name not in container_definitions:
            issues.append(
                DiagnosticIssue(
                    "container.definition_missing",
                    f"container intent {intent.request_id} references unknown container "
                    f"{intent.container_name}",
                )
            )
    for result in persistence.container_operation_results():
        if result.container_name not in container_definitions:
            issues.append(
                DiagnosticIssue(
                    "container.definition_missing",
                    f"container result {result.request_id} references unknown container "
                    f"{result.container_name}",
                )
            )

    preemptive_definitions = {
        definition.name
        for definition in persistence.preemptive_resource_definitions()
    }
    for demand in persistence.preemptive_resource_demands():
        if demand.resource_name not in preemptive_definitions:
            issues.append(
                DiagnosticIssue(
                    "preemptive.definition_missing",
                    f"preemptive demand {demand.request_id} references unknown "
                    f"resource {demand.resource_name}",
                )
            )
    for reservation in persistence.preemptive_resource_reservations():
        if reservation.resource_name not in preemptive_definitions:
            issues.append(
                DiagnosticIssue(
                    "preemptive.definition_missing",
                    f"preemptive reservation {reservation.reservation_id} references "
                    f"unknown resource {reservation.resource_name}",
                )
            )
    for intent in persistence.preemptive_resource_release_intents():
        if intent.resource_name not in preemptive_definitions:
            issues.append(
                DiagnosticIssue(
                    "preemptive.definition_missing",
                    f"preemptive release intent {intent.intent_id} references unknown "
                    f"resource {intent.resource_name}",
                )
            )

    scenario_state = persistence.scenario_state()
    counts = RuntimeCounts(
        events=len(persistence.events()),
        scheduled_work=len(scheduled),
        scenario_decisions=0 if scenario_state is None else len(scenario_state.decisions),
        scenario_activations=0 if scenario_state is None else len(scenario_state.activations),
        resource_demands=len(persistence.resource_demands()),
        resource_reservations=len(persistence.resource_reservations()),
        resource_release_intents=len(persistence.resource_release_intents()),
        store_items=len(persistence.store_items()),
        store_put_intents=len(persistence.store_put_intents()),
        store_get_requests=len(persistence.store_get_requests()),
        store_get_results=len(persistence.store_get_results()),
        container_operation_intents=len(persistence.container_operation_intents()),
        container_operation_results=len(persistence.container_operation_results()),
        preemptive_resource_demands=len(persistence.preemptive_resource_demands()),
        preemptive_resource_reservations=len(
            persistence.preemptive_resource_reservations()
        ),
        preemptive_resource_release_intents=len(
            persistence.preemptive_resource_release_intents()
        ),
        resource_preemption_results=len(persistence.resource_preemption_results()),
    )
    return RuntimeDiagnostics(
        position=persistence.simulation_position(),
        counts=counts,
        issues=tuple(issues),
    )
