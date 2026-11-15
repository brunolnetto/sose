from __future__ import annotations

from collections.abc import Iterable  # noqa: F401 - re-exported for compatibility
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy  # noqa: F401 - re-exported for compatibility
from dataclasses import dataclass, field, fields
from datetime import datetime  # noqa: F401 - re-exported for compatibility
from typing import Iterator

from sose.core.events import Command, DomainEvent
from sose.core.runtime import (
    ResourceDefinition,
    ResourceDemand,
    ResourceReleaseIntent,
    ResourceReservation,
    StoreDefinition,
    DurableStoreItem,
    StorePutIntent,
    StoreGetRequest,
    StoreGetResult,
    ContainerDefinition,
    ContainerState,
    ContainerOperationIntent,
    ContainerOperationResult,
    PreemptiveResourceDefinition,
    PreemptiveResourceDemand,
    PreemptiveResourceReservation,
    PreemptiveResourceReleaseIntent,
    ResourcePreemptionResult,
    ScheduledWork,
    SimulationPosition,
)
from sose.domain.entity import Entity
from sose.domain.delivery import DomainDelivery
from sose.composition.model import BoundaryConsumption, BoundaryDelivery, BoundaryMessage
from sose.composition.effects import BusinessEffectApplied
from sose.composition.bindings import CustomerSettlementBinding
from sose.scenarios.model import ScenarioRuntimeState
from sose.jobs.model import SimulationJobState
from sose.sinks.model import SinkCheckpoint, SinkDelivery


from .memory_reads import MemoryCommittedReadsMixin
from .memory_uow_boundary import BoundaryRecordsUnitOfWorkMixin
from .memory_uow_core import CoreRecordsUnitOfWorkMixin
from .memory_uow_resources import ResourceRecordsUnitOfWorkMixin
from .memory_uow_stores import StoreContainerRecordsUnitOfWorkMixin


@dataclass
class _State:
    entities: dict[tuple[str, str], Entity] = field(default_factory=dict)
    events: list[DomainEvent] = field(default_factory=list)
    commands: dict[str, Command] = field(default_factory=dict)
    scheduled_work: dict[str, ScheduledWork] = field(default_factory=dict)
    simulation_positions: dict[str, SimulationPosition] = field(default_factory=dict)
    scenario_states: dict[str, ScenarioRuntimeState] = field(default_factory=dict)
    resource_definitions: dict[str, ResourceDefinition] = field(default_factory=dict)
    resource_demands: dict[str, ResourceDemand] = field(default_factory=dict)
    resource_reservations: dict[str, ResourceReservation] = field(default_factory=dict)
    resource_release_intents: dict[str, ResourceReleaseIntent] = field(default_factory=dict)
    store_definitions: dict[str, StoreDefinition] = field(default_factory=dict)
    store_items: dict[str, DurableStoreItem] = field(default_factory=dict)
    store_put_intents: dict[str, StorePutIntent] = field(default_factory=dict)
    store_get_requests: dict[str, StoreGetRequest] = field(default_factory=dict)
    store_get_results: dict[str, StoreGetResult] = field(default_factory=dict)
    container_definitions: dict[str, ContainerDefinition] = field(default_factory=dict)
    container_states: dict[str, ContainerState] = field(default_factory=dict)
    container_operation_intents: dict[str, ContainerOperationIntent] = field(default_factory=dict)
    container_operation_results: dict[str, ContainerOperationResult] = field(default_factory=dict)
    preemptive_resource_definitions: dict[str, PreemptiveResourceDefinition] = field(
        default_factory=dict
    )
    preemptive_resource_demands: dict[str, PreemptiveResourceDemand] = field(default_factory=dict)
    preemptive_resource_reservations: dict[str, PreemptiveResourceReservation] = field(
        default_factory=dict
    )
    preemptive_resource_release_intents: dict[str, PreemptiveResourceReleaseIntent] = field(
        default_factory=dict
    )
    resource_preemption_results: dict[str, ResourcePreemptionResult] = field(default_factory=dict)
    job_states: dict[str, SimulationJobState] = field(default_factory=dict)
    sink_deliveries: dict[str, SinkDelivery] = field(default_factory=dict)
    sink_checkpoints: dict[tuple[str, str], SinkCheckpoint] = field(default_factory=dict)
    boundary_messages: dict[str, BoundaryMessage] = field(default_factory=dict)
    boundary_deliveries: dict[str, BoundaryDelivery] = field(default_factory=dict)
    boundary_consumptions: dict[str, BoundaryConsumption] = field(default_factory=dict)
    business_effects: dict[str, BusinessEffectApplied] = field(default_factory=dict)
    customer_settlement_bindings: dict[tuple[str, str], CustomerSettlementBinding] = field(
        default_factory=dict
    )
    domain_deliveries: dict[str, DomainDelivery] = field(default_factory=dict)
    committed_ticks: dict[str, int] = field(default_factory=dict)


def fork_state(state: _State) -> _State:
    """Create a transaction-local structural copy of durable state.

    Top-level mutable collections are copied, while existing record objects are
    shared until a UnitOfWork explicitly reads/saves them through methods that
    already deepcopy values. This avoids an O(total object graph) deepcopy at
    transaction entry without weakening rollback isolation.
    """

    values: dict[str, object] = {}
    for field_info in fields(_State):
        value = getattr(state, field_info.name)
        if isinstance(value, dict):
            values[field_info.name] = dict(value)
        elif isinstance(value, list):
            values[field_info.name] = list(value)
        else:
            values[field_info.name] = value
    return _State(**values)


class MemoryUnitOfWork(
    BoundaryRecordsUnitOfWorkMixin,
    CoreRecordsUnitOfWorkMixin,
    ResourceRecordsUnitOfWorkMixin,
    StoreContainerRecordsUnitOfWorkMixin,
):
    def __init__(self, working: _State, owner: "MemoryPersistence") -> None:
        self._working = working
        self._owner = owner
        self._closed = False
        self._dirty_records: set[tuple[str, object]] = set()

    @property
    def dirty_records(self) -> frozenset[tuple[str, object]]:
        """Internal record identities touched by this unit of work."""
        return frozenset(self._dirty_records)

    def _mark_dirty(self, collection: str, key: object) -> None:
        self._dirty_records.add((collection, key))

    def commit(self) -> None:
        store_overlap = (
            self._working.store_get_requests.keys() & self._working.store_get_results.keys()
        )
        if store_overlap:
            request_id = sorted(store_overlap)[0]
            raise ValueError(f"store get identity cannot be pending and completed: {request_id}")
        container_overlap = (
            self._working.container_operation_intents.keys()
            & self._working.container_operation_results.keys()
        )
        if container_overlap:
            request_id = sorted(container_overlap)[0]
            raise ValueError(
                f"container operation identity cannot be pending and completed: {request_id}"
            )
        self._owner._state = self._working
        self._closed = True

    def rollback(self) -> None:
        self._closed = True


@dataclass(slots=True)
class _TransactionLifetime:
    """Shared mutable lifetime across copied asyncio ContextVar contexts."""

    active: bool = True


def _transaction_tracker(store):
    """Lazy bootstrap for explicit adapter/coverage probes skipping __init__."""
    tracker = getattr(store, "_transaction_depth", None)
    if tracker is None:
        tracker = ContextVar(
            f"sose-memory-transaction-depth-{id(store)}",
            default=(),
        )
        store._transaction_depth = tracker
    return tracker


def tracked_transaction_depth(factory):
    """Track the live transaction context in Memory-derived persistence adapters.

    Overrides that construct their own UoW must opt in as well; otherwise a
    nested queue service can fork/commit an inner snapshot that disagrees with
    its outer UoW (especially the incremental JSONL journal).
    """

    @contextmanager
    def scope(self, *args, **kwargs):
        lifetime = _TransactionLifetime()
        tracker = _transaction_tracker(self)
        inherited = tracker.get()
        token = tracker.set((*inherited, lifetime))
        try:
            with factory(self, *args, **kwargs) as uow:
                yield uow
        finally:
            # Child tasks may inherit this ContextVar by reference. Mark the
            # shared lifetime dead BEFORE resetting the parent's context.
            lifetime.active = False
            tracker.reset(token)

    return scope


class MemoryPersistence(MemoryCommittedReadsMixin):
    def __init__(self) -> None:
        self._state = _State()
        # Queryable per async/thread context. Memory UoW forks must never be
        # mistaken for nested commits: the outer fork can overwrite the inner.
        self._transaction_depth = ContextVar(
            f"sose-memory-transaction-depth-{id(self)}",
            default=(),
        )
        self._active_read_snapshot = ContextVar(
            f"sose-memory-read-snapshot-{id(self)}",
            default=None,
        )
        self._read_only = False

    @property
    def transaction_depth(self) -> int:
        return sum(lifetime.active for lifetime in _transaction_tracker(self).get())

    def _snapshot_state(self) -> _State:
        """Return the state version observed by a read-snapshot operation."""
        return self._state

    @staticmethod
    def _read_view(state: _State) -> "MemoryPersistence":
        """Build an in-memory read facade without copying the captured state."""
        view = MemoryPersistence()
        view._state = state
        view._read_only = True
        return view

    @contextmanager
    def read_snapshot(self) -> Iterator["MemoryPersistence"]:
        """Yield one detached read-only view for this context and nested scopes."""
        active = self._active_read_snapshot.get()
        if active is not None:
            yield active
            return
        if self.transaction_depth:
            raise RuntimeError("cannot open read snapshot inside a write transaction")

        view = self._read_view(self._snapshot_state())
        token = self._active_read_snapshot.set(view)
        try:
            yield view
        finally:
            self._active_read_snapshot.reset(token)

    @tracked_transaction_depth
    @contextmanager
    def transaction(self, *, owner_id: str | None = None) -> Iterator[MemoryUnitOfWork]:
        if self._read_only:
            raise RuntimeError("read snapshot is read-only")
        if owner_id is not None and not owner_id:
            raise ValueError("owner_id must not be empty")
        uow = MemoryUnitOfWork(fork_state(self._state), self)
        try:
            yield uow
            if not uow._closed:
                uow.commit()
        except Exception:
            uow.rollback()
            raise

    def boundary_protocol_snapshot(
        self,
    ) -> tuple[
        tuple[BoundaryMessage, ...], tuple[BoundaryDelivery, ...], tuple[BoundaryConsumption, ...]
    ]:
        """Read three protocol families from one immutable in-memory snapshot.

        This never opens a mutation transaction or publishes the forked state.
        Persistent subclasses override it with a coherent database snapshot.
        """
        uow = MemoryUnitOfWork(fork_state(self._state), self)
        try:
            return (
                uow.boundary_messages(),
                uow.boundary_deliveries(),
                uow.boundary_consumptions(),
            )
        finally:
            uow.rollback()
