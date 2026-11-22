"""Canonical, owner-qualified snapshot of an admitted organization (#496).

One coherent ``read_snapshot()`` view of the namespace is projected onto the
admitted definition's components. Nothing is written and no organization-wide
clock, aggregate entity or super-StateChart is created.

Per component (``owner_id``) the snapshot holds:

- **machines**: the StateChart entities this owner transitioned. Entities carry no
  owner, so a machine is attributed by the owner of its transition events. Its
  state is the ``to_state`` of the owner's last transition, and it must equal the
  stored entity state. A machine transitioned by two owners, or whose stored state
  contradicts its last transition, fails closed. Entities of the components' types
  with no transition by any of the organization's owners are listed once, as
  ``unattributed_machines``: they carry no owner evidence;
- **transition_event_ids** (commit order), and the owner's **scheduled_work** with
  the **pending_commands** it names (a scheduled work naming a missing command
  fails closed);
- **jobs**: the declared jobs' checkpoints, including whether each is ``attested``.

For the organization it holds the owner-qualified (v2) **boundary** messages,
their delivery status and consumption, and a ``digest``: SHA-256 over the
canonical JSON of everything else. The same durable state yields the same digest
on every adapter and after a restart.

Not included: resources and physical pools (admission still refuses physical pools),
operational policy/calendar/cost references, and any claim of authority (#545).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import TYPE_CHECKING

from sose.persistence.base import ReadSnapshot

from .job_observation import DeclaredJobCheckpoint, observe_organization_jobs
from .observation import _component_entity_types, _is_observed_transition
from .organization import OrganizationDefinition, validate_organization_admission

if TYPE_CHECKING:
    from sose.persistence.base import Persistence


@dataclass(frozen=True, slots=True)
class MachineState:
    entity_type: str
    entity_id: str
    state: str
    last_event_id: str


@dataclass(frozen=True, slots=True)
class PendingCommand:
    command_id: str
    name: str
    entity_type: str
    entity_id: str
    due_at: str


@dataclass(frozen=True, slots=True)
class ComponentSnapshot:
    component_id: str
    domain: str
    owner_id: str
    machines: tuple[MachineState, ...]
    transition_event_ids: tuple[str, ...]
    pending_commands: tuple[PendingCommand, ...]
    scheduled_work_ids: tuple[str, ...]
    jobs: tuple[DeclaredJobCheckpoint, ...]


@dataclass(frozen=True, slots=True)
class BoundaryRecord:
    message_id: str
    contract_name: str
    contract_version: int
    source_component_id: str
    destination_component_id: str
    definition_fingerprint: str
    delivery_statuses: tuple[str, ...]
    consumed: bool


@dataclass(frozen=True, slots=True)
class OrganizationSnapshot:
    organization_id: str
    definition_fingerprint: str
    version: int
    config_epoch: int
    components: tuple[ComponentSnapshot, ...]
    unattributed_machines: tuple[tuple[str, str, str], ...]
    boundary: tuple[BoundaryRecord, ...]
    digest: str


def snapshot_organization(
    definition: OrganizationDefinition,
    persistence: "Persistence",
    *,
    catalog,
    manifests,
    consumers,
) -> OrganizationSnapshot:
    """Project one coherent read of ``persistence`` onto the admitted components."""
    if not isinstance(persistence, ReadSnapshot):
        raise TypeError("a canonical organization snapshot requires read_snapshot()")
    fingerprint = validate_organization_admission(
        definition, catalog=catalog, manifests=manifests, consumers=consumers,
    )
    components = tuple(sorted(definition.components, key=lambda c: c.component_id))
    owner_ids = [component.owner_id for component in components]
    if len(owner_ids) != len(set(owner_ids)):
        raise ValueError("an owner-qualified snapshot requires distinct component owner_id")
    types_by_owner = _component_entity_types(components, catalog)
    with persistence.read_snapshot() as view:
        jobs = observe_organization_jobs(
            definition, view, catalog=catalog, manifests=manifests, consumers=consumers,
        ).jobs
        machines, transitions, unattributed = _machines(view, types_by_owner)
        built = tuple(
            _component(view, component, machines, transitions, jobs)
            for component in components
        )
        boundary = _boundary(view, definition.organization_id)
    body = dict(
        organization_id=definition.organization_id, definition_fingerprint=fingerprint,
        version=definition.version, config_epoch=definition.config_epoch,
        components=built, unattributed_machines=unattributed, boundary=boundary,
    )
    return OrganizationSnapshot(**body, digest=_digest(body))


def _machines(view, types_by_owner):
    """Owner -> machines and transition ids, from the owner's transition events."""
    last, transitions = _last_transitions(view.events(), types_by_owner)
    stored = {(entity.entity_type, entity.id): entity.state for entity in view.entities()}
    machines: dict[str, list[MachineState]] = {owner: [] for owner in types_by_owner}
    for key, (owner, event_id, state) in sorted(last.items()):
        if stored.get(key) != state:
            raise ValueError(
                f"stored state of machine {key} contradicts its owner's last transition"
            )
        machines[owner].append(MachineState(key[0], key[1], state, event_id))
    known_types = set().union(*types_by_owner.values())
    unattributed = tuple(sorted(
        (entity_type, entity_id, state) for (entity_type, entity_id), state in stored.items()
        if entity_type in known_types and (entity_type, entity_id) not in last
    ))
    return machines, transitions, unattributed


def _last_transitions(events, types_by_owner):
    """(entity_type, entity_id) -> (owner, event_id, to_state) of its last transition."""
    last: dict[tuple[str, str], tuple[str, str, str]] = {}
    transitions: dict[str, list[str]] = {owner: [] for owner in types_by_owner}
    for event in events:
        if event.entity_type not in types_by_owner.get(event.owner_id, ()):
            continue
        if not _is_observed_transition(event):
            continue
        key = (event.entity_type, event.entity_id)
        if key in last and last[key][0] != event.owner_id:
            raise ValueError(f"machine {key} was transitioned by two owners")
        last[key] = (event.owner_id, event.event_id, event.payload["to_state"])
        transitions[event.owner_id].append(event.event_id)
    return last, transitions


def _component(view, component, machines, transitions, jobs) -> ComponentSnapshot:
    owner = component.owner_id
    work = [item for item in view.scheduled_work() if item.owner_id == owner]
    commands = []
    for item in work:
        # A pending Command is reached through the owner's durable ScheduledWork.
        command = view.command(item.command_id)
        if command is None:
            raise ValueError(f"scheduled work {item.work_id} names a missing command")
        commands.append(PendingCommand(
            command.command_id, command.name, command.entity_type, command.entity_id,
            command.due_at.isoformat(),
        ))
    return ComponentSnapshot(
        component_id=component.component_id, domain=component.domain, owner_id=owner,
        machines=tuple(machines[owner]),
        transition_event_ids=tuple(transitions[owner]),
        pending_commands=tuple(sorted(commands, key=lambda command: command.command_id)),
        scheduled_work_ids=tuple(sorted(item.work_id for item in work)),
        jobs=tuple(job for job in jobs if job.component_id == component.component_id),
    )


def _boundary(view, organization_id: str) -> tuple[BoundaryRecord, ...]:
    messages, deliveries, consumptions = view.boundary_protocol_snapshot()
    consumed = {consumption.message_id for consumption in consumptions}
    statuses: dict[str, list[str]] = {}
    for delivery in deliveries:
        statuses.setdefault(delivery.message_id, []).append(str(delivery.status.value))
    return tuple(sorted(
        (
            BoundaryRecord(
                message_id=message.message_id, contract_name=message.contract_name,
                contract_version=message.contract_version,
                source_component_id=message.source_component_id,
                destination_component_id=message.destination_component_id,
                definition_fingerprint=message.definition_fingerprint,
                delivery_statuses=tuple(sorted(statuses.get(message.message_id, ()))),
                consumed=message.message_id in consumed,
            )
            for message in messages if message.organization_id == organization_id
        ),
        key=lambda record: record.message_id,
    ))


def _digest(body: dict) -> str:
    canonical = json.dumps(
        {name: _plain(value) for name, value in body.items()},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _plain(value):
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return {name: _plain(item) for name, item in asdict(value).items()}
    return value
