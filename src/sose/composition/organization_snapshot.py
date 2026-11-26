"""Canonical, owner-qualified snapshot of an admitted organization (#496).

One coherent ``read_snapshot()`` view of the namespace is projected onto the
admitted definition's components. Nothing is written and no organization-wide
clock, aggregate entity or super-StateChart is created.

Per component (``owner_id``) the snapshot holds:

- **machines**: the StateChart entities this owner transitioned. Entities carry no
  owner, so a machine is attributed by the owner of its transition events. Its
  state is the ``to_state`` of the owner's last transition, and it must equal the
  stored entity state. It fails closed when ANY transition of the machine belongs to
  another owner (for example another organization in the namespace), when an owner
  of this organization moves a StateChart type of another component (attributed or
  not), or when the stored state contradicts its last transition. Entities of the components' types
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

Not included: the job-local resource queue (it has no owner); physical pools only with
``physical_ledger`` (below),
operational policy/calendar/cost references, and any claim of authority (#545).
Refused: components sharing one ``owner_id`` (owner-qualified attribution needs
distinct owners), and jobs whose entities live in a domain warehouse rather than the
namespace (their transitioned machines have no stored state here and fail closed).
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
import json
from typing import TYPE_CHECKING

from sose.core.attestation_chain import ATTRIBUTES_EVENT
from sose.core.transition_authority import ATTESTATION_EVENT
from sose.persistence.base import ReadSnapshot

from .job_observation import DeclaredJobCheckpoint, observe_organization_jobs
from .observation import _component_entity_types, _is_observed_transition
from .organization import OrganizationDefinition, validate_organization_admission
from .physical_pools import PoolSnapshot, pool_snapshots, read_physical_ledger

if TYPE_CHECKING:
    from sose.persistence.base import Persistence


@dataclass(frozen=True, slots=True)
class MachineState:
    entity_type: str
    entity_id: str
    state: str
    last_event_id: str
    # True when a TransitionAuthority verified this machine's receipt chain (#545).
    authenticated: bool = False


@dataclass(frozen=True, slots=True)
class PendingCommand:
    command_id: str
    name: str
    entity_type: str
    entity_id: str
    due_at: str


@dataclass(frozen=True, slots=True)
class QueueRecord:
    """One record of a component job's own resource queue (#496)."""

    kind: str                  # "demand", "reservation" or "release_intent"
    record_id: str             # request, reservation or intent id
    resource_name: str
    sequence: int | None
    at: str


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
    # Records stamped with this owner; in the digest only when nonempty.
    resource_queue: tuple[QueueRecord, ...] = ()


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
    authenticated_key_id: str | None
    digest: str
    # Admitted physical pools (#496); in the digest only when the organization has any.
    physical_pools: tuple[PoolSnapshot, ...] = field(default=())


def snapshot_organization(
    definition: OrganizationDefinition,
    persistence: "Persistence",
    *,
    catalog,
    manifests,
    consumers,
    transition_authority=None,
    physical_ledger: bool = False,
) -> OrganizationSnapshot:
    """Project one coherent read of ``persistence`` onto the admitted components.

    With ``transition_authority`` (#545), every attributed machine's transitions
    must form a chain of HMAC receipts issued by that authority and ending at the
    stored entity; a forged, unsigned or rewritten step fails closed, and every
    machine is marked ``authenticated``.

    With ``physical_ledger`` (PostgreSQL only), the declared physical pools are
    admitted against the resource ledger, read in the same REPEATABLE READ
    transaction as the rest of the snapshot, and listed in ``physical_pools``.
    """
    if not isinstance(persistence, ReadSnapshot):
        raise TypeError("a canonical organization snapshot requires read_snapshot()")
    components = tuple(sorted(definition.components, key=lambda c: c.component_id))
    owner_ids = [component.owner_id for component in components]
    if len(owner_ids) != len(set(owner_ids)):
        raise ValueError("an owner-qualified snapshot requires distinct component owner_id")
    # Only a definition that declares pools reads the ledger.
    reads_ledger = physical_ledger and bool(definition.physical_pools)
    with _coherent_read(persistence, reads_ledger) as (view, ledger):
        fingerprint = validate_organization_admission(
            definition, catalog=catalog, manifests=manifests, consumers=consumers,
            physical_ledger=ledger,
        )
        types_by_owner = _component_entity_types(components, catalog)
        jobs = observe_organization_jobs(
            definition, view, catalog=catalog, manifests=manifests, consumers=consumers,
            physical_ledger=ledger,
        ).jobs
        machines, transitions, unattributed = _machines(
            view, types_by_owner, transition_authority,
        )
        work_by_owner: dict[str, list] = {}
        for item in view.scheduled_work():
            work_by_owner.setdefault(item.owner_id, []).append(item)
        queues = _resource_queues(view)
        built = tuple(
            replace(
                _component(view, component, machines, transitions, jobs, work_by_owner),
                resource_queue=queues.get(component.owner_id, ()),
            )
            for component in components
        )
        boundary = _boundary(view, definition.organization_id)
    body = dict(
        organization_id=definition.organization_id, definition_fingerprint=fingerprint,
        version=definition.version, config_epoch=definition.config_epoch,
        components=built, unattributed_machines=unattributed, boundary=boundary,
        authenticated_key_id=(
            None if transition_authority is None else transition_authority.key_id
        ),
    )
    pools = () if ledger is None else pool_snapshots(definition, ledger)
    digested = {**body, "physical_pools": pools} if pools else body
    return OrganizationSnapshot(**body, digest=_digest(digested), physical_pools=pools)


@contextmanager
def _coherent_read(persistence, physical_ledger: bool):
    """One read view, plus the physical ledger read in the same transaction."""
    if not physical_ledger:
        with persistence.read_snapshot() as view:
            yield view, None
        return
    if getattr(persistence, "_active_read_snapshot", None) is not None and (
        persistence._active_read_snapshot.get() is not None
    ):
        raise RuntimeError("a physical-pool snapshot cannot reuse an outer read snapshot")
    connection = getattr(persistence, "_connection", None)
    if connection is None or not callable(getattr(persistence, "temporal_resources", None)):
        raise TypeError("physical pools in a snapshot require the PostgreSQL resource ledger")
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        ledger = read_physical_ledger(persistence)
        with persistence.read_snapshot() as view:
            yield view, ledger


def _machines(view, types_by_owner, authority=None):
    """Owner -> machines and transition ids, from the owner's transition events."""
    events = view.events()
    last, transitions = _last_transitions(events, types_by_owner)
    authenticated = _authenticated(view, events, last, authority)
    stored = {(entity.entity_type, entity.id): entity.state for entity in view.entities()}
    machines: dict[str, list[MachineState]] = {owner: [] for owner in types_by_owner}
    for key, (owner, event_id, state) in sorted(last.items()):
        if stored.get(key) != state:
            raise ValueError(
                f"stored state of machine {key} contradicts its owner's last transition"
            )
        machines[owner].append(
            MachineState(key[0], key[1], state, event_id, key in authenticated),
        )
    known_types = set().union(*types_by_owner.values())
    unattributed = tuple(sorted(
        (entity_type, entity_id, state) for (entity_type, entity_id), state in stored.items()
        if entity_type in known_types and (entity_type, entity_id) not in last
    ))
    return machines, transitions, unattributed


def _authenticated(view, events, last, authority) -> set:
    """Keys of the attributed machines whose receipt chains ``authority`` verified.

    Each machine's chain is its transitions, their receipts and its attribute
    receipts (#494), in commit order. Only events of the attributed machines are
    read, so a stray receipt elsewhere in the namespace cannot deny the snapshot.
    """
    if authority is None:
        return set()
    chain_events = {key: [] for key in last}
    for event in events:
        key = (event.entity_type, event.entity_id)
        if key in chain_events and event.name in _CHAIN_EVENTS:
            chain_events[key].append(event)
    for key in last:
        entity = view.entity(*key)
        if entity is None:
            raise ValueError(f"attributed machine {key} has no stored entity")
        authority.verify_history(events=chain_events[key], entity=entity)
    return set(last)


_CHAIN_EVENTS = frozenset({"entity.state_transition", ATTESTATION_EVENT, ATTRIBUTES_EVENT})


def _last_transitions(events, types_by_owner):
    """(entity_type, entity_id) -> (owner, event_id, to_state) of its last transition.

    Every state-transition event counts toward who moved a machine, whatever its
    owner (another organization in the namespace, or an owner of this one moving a
    type its component does not declare). A machine is attributed only when all of
    its transitions belong to one admitted owner of a component declaring the type.
    """
    last: dict[tuple[str, str], tuple[str, str, str]] = {}
    movers: dict[tuple[str, str], set] = {}
    transitions: dict[str, list[str]] = {owner: [] for owner in types_by_owner}
    known_types = set().union(*types_by_owner.values())
    for event in events:
        if event.name != "entity.state_transition":
            continue
        key = (event.entity_type, event.entity_id)
        movers.setdefault(key, set()).add(event.owner_id)
        declared = types_by_owner.get(event.owner_id)
        _require_declared(event, declared, known_types)
        if event.entity_type not in (declared or ()):
            continue
        if not _is_observed_transition(event):
            continue
        last[key] = (event.owner_id, event.event_id, event.payload["to_state"])
        transitions[event.owner_id].append(event.event_id)
    for key, (owner, _event_id, _state) in last.items():
        if movers[key] != {owner}:
            raise ValueError(f"machine {key} was transitioned by two owners")
    return last, transitions


def _require_declared(event, declared, known_types) -> None:
    """As in observe_organization_events: an admitted owner moving another
    component's StateChart type contradicts the admitted definition."""
    if declared is not None and event.entity_type in known_types - declared:
        raise ValueError(
            f"owner {event.owner_id} moved {event.entity_type}, a type its component "
            "does not declare"
        )


def _component(view, component, machines, transitions, jobs, work_by_owner) -> ComponentSnapshot:
    owner = component.owner_id
    work = work_by_owner.get(owner, [])
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


def _resource_queues(view) -> dict[str, tuple[QueueRecord, ...]]:
    """Owner -> its stamped demands, reservations and release intents."""
    records: dict[str, list[QueueRecord]] = {}
    for demand in view.resource_demands():
        _add(records, demand.owner_id, QueueRecord(
            "demand", demand.request_id, demand.resource_name, demand.sequence,
            demand.requested_at.isoformat()))
    for reservation in view.resource_reservations():
        _add(records, reservation.owner_id, QueueRecord(
            "reservation", reservation.reservation_id, reservation.resource_name,
            reservation.sequence, reservation.acquired_at.isoformat()))
    for intent in view.resource_release_intents():
        _add(records, intent.owner_id, QueueRecord(
            "release_intent", intent.intent_id, intent.resource_name, None,
            intent.requested_at.isoformat()))
    return {owner: tuple(sorted(items, key=lambda r: (r.kind, r.record_id)))
            for owner, items in records.items()}


def _add(records: dict, owner: str | None, record: QueueRecord) -> None:
    if owner is not None:
        records.setdefault(owner, []).append(record)


def _digest(body: dict) -> str:
    canonical = json.dumps(
        {name: _plain(value) for name, value in body.items()},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _plain(value):
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, ComponentSnapshot):
        plain = {name: _plain(getattr(value, name)) for name in value.__dataclass_fields__}
        if not value.resource_queue:
            del plain["resource_queue"]    # keeps the digest of snapshots without queues
        return plain
    if hasattr(value, "__dataclass_fields__"):
        return {name: _plain(item) for name, item in asdict(value).items()}
    return value
