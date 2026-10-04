from __future__ import annotations

from types import SimpleNamespace

from sose.core.stores import DurableStoreManager
from sose.core.runtime import StoreDefinition
from sose.examples.canonical import common as canonical_common
from sose.examples.canonical import dining_philosophers
from sose.examples.canonical import job_shop
from sose.examples.canonical import producer_consumer
from sose.examples.canonical import readers_writers
from sose.examples.canonical import sleeping_barber
from sose.persistence.memory import MemoryPersistence


def _reservation(request_id: str, *, resource_name: str = "r", sequence: int = 0):
    return SimpleNamespace(
        request_id=request_id,
        resource_name=resource_name,
        sequence=sequence,
        reservation_id=f"res-{request_id}",
    )


class _Resources:
    def __init__(self, reservations: dict[str, object] | None = None):
        self.reservations = dict(reservations or {})
        self.requests = set(self.reservations)
        self.released: list[str] = []
        self.ensured: list[str] = []

    def reservation_for(self, request_id: str):
        return self.reservations.get(request_id)

    def has_request(self, request_id: str) -> bool:
        return request_id in self.requests

    def ensure_requested(self, backend, *, resource_name: str, request_id: str, requested_at, priority=None):
        self.requests.add(request_id)
        self.ensured.append(request_id)
        if request_id not in self.reservations:
            self.reservations[request_id] = _reservation(
                request_id,
                resource_name=resource_name,
                sequence=len(self.reservations),
            )

    def release(self, backend, reservation_id: str):
        self.released.append(reservation_id)
        for key, value in list(self.reservations.items()):
            if value.reservation_id == reservation_id:
                self.reservations.pop(key, None)


def test_dining_philosophers_seed_and_helper_edges(monkeypatch):
    persistence = SimpleNamespace(
        resource_definitions=lambda: (SimpleNamespace(name="fork-0"),),
    )
    monkeypatch.setattr(
        dining_philosophers,
        "seed_case",
        lambda *args, **kwargs: "seeded",
    )
    config = dining_philosophers.DiningPhilosophersConfig(participants=3)
    assert dining_philosophers.seed(persistence, config) == "seeded"

    resources = _Resources()
    assert dining_philosophers._active_candidate(resources, 2) == "active:noop"

    resources = _Resources(
        {
            "p0-fork-0": _reservation("p0-fork-0", resource_name="fork-0"),
            "p0-fork-1": _reservation("p0-fork-1", resource_name="fork-1"),
        }
    )
    dining_philosophers._release_philosopher_if_holding(
        resources=resources,
        backend=object(),
        philosopher=0,
        participants=2,
        requested_at=object(),
    )
    assert len(resources.released) >= 1

    resources = _Resources(
        {"p0-fork-1": _reservation("p0-fork-1", resource_name="fork-1")}
    )
    dining_philosophers._release_philosopher_if_holding(
        resources=resources,
        backend=object(),
        philosopher=0,
        participants=2,
        requested_at=object(),
    )
    assert resources.released

    assert (
        dining_philosophers._candidate_for_state(
            _Resources(),
            current=SimpleNamespace(state="done"),
            participants=2,
        )
        == "noop"
    )

    transitioned: list[str] = []
    monkeypatch.setattr(
        dining_philosophers,
        "transition",
        lambda engine, current, event: transitioned.append(event),
    )
    dining_philosophers._reconcile_ready_action(
        SimpleNamespace(context=SimpleNamespace(clock=SimpleNamespace(now=object()))),
        object(),
        current=SimpleNamespace(state="active"),
        resources=_Resources(),
        participants=2,
    )
    dining_philosophers._reconcile_active_action(
        SimpleNamespace(
            resource_demands=lambda: (),
            resource_reservations=lambda: (),
        ),
        SimpleNamespace(context=SimpleNamespace(clock=SimpleNamespace(now=object()))),
        object(),
        current=SimpleNamespace(state="active"),
        action="active:noop",
        resources=_Resources(),
        participants=2,
    )
    assert "finish" in transitioned

    dining_philosophers._reconcile_active_action(
        SimpleNamespace(
            resource_demands=lambda: ("d",),
            resource_reservations=lambda: (),
        ),
        SimpleNamespace(context=SimpleNamespace(clock=SimpleNamespace(now=object()))),
        object(),
        current=SimpleNamespace(state="ready"),
        action="active:noop",
        resources=_Resources(),
        participants=2,
    )


def test_dining_philosophers_reconcile_noop_action_exits(monkeypatch):
    current = SimpleNamespace(state="active", id="C1")
    persistence = SimpleNamespace(entity=lambda *args, **kwargs: current)
    engine = SimpleNamespace(
        resources=_Resources(),
        context=SimpleNamespace(clock=SimpleNamespace(tick=1, now=object())),
    )
    config = dining_philosophers.DiningPhilosophersConfig(enabled=True, participants=2)
    monkeypatch.setattr(
        dining_philosophers,
        "resolve_tick_action",
        lambda *args, **kwargs: "noop",
    )
    dining_philosophers.reconcile(
        persistence,
        engine,
        object(),
        config,
        SimpleNamespace(id="C1"),
    )


def test_job_shop_seed_candidate_and_reconcile_exit_edges(monkeypatch):
    persistence = SimpleNamespace(
        resource_definitions=lambda: (SimpleNamespace(name="machine-0"),),
        store_definitions=lambda: (SimpleNamespace(name="completed_operations"),),
    )
    monkeypatch.setattr(job_shop, "seed_case", lambda *args, **kwargs: "seeded")
    config = job_shop.JobShopConfig(participants=2, machines=1)
    assert job_shop.seed(persistence, config) == "seeded"

    noop_candidate = job_shop._candidate_state(
        current_state="done",
        resources=_Resources(),
        persistence=SimpleNamespace(store_items=lambda: ()),
        participants=2,
    )
    assert noop_candidate == "noop"

    class _StoreRecorder:
        def __init__(self):
            self.markers: list[str] = []

        def ensure_put(self, backend, *, store_name, item_id, value, requested_at):
            self.markers.append(item_id)

    stores = _StoreRecorder()
    resources = _Resources()
    job_shop._release_completed_operation(
        action="job-1-op-0",
        resources=resources,
        stores=stores,
        backend=object(),
        requested_at=object(),
    )
    assert stores.markers == ["job-1-op-0-done"]
    assert resources.released == []

    persistence = SimpleNamespace(
        store_items=lambda: (
            SimpleNamespace(store_name="completed_operations", item_id="job-0-op-1-done"),
            SimpleNamespace(store_name="completed_operations", item_id="job-1-op-0-done"),
        )
    )
    resources = _Resources()
    resources.requests.add("job-1-op-1")
    job_shop._admit_successors(
        persistence=persistence,
        resources=resources,
        backend=object(),
        requested_at=object(),
        participants=2,
        machines=2,
    )
    assert resources.ensured == []

    current = SimpleNamespace(state="active", id="C1")
    persistence = SimpleNamespace(entity=lambda *args, **kwargs: current, store_items=lambda: ())
    engine = SimpleNamespace(
        resources=_Resources(),
        context=SimpleNamespace(clock=SimpleNamespace(tick=1, now=object())),
    )
    monkeypatch.setattr(job_shop, "resolve_tick_action", lambda *args, **kwargs: "ready")
    job_shop.reconcile(
        persistence,
        engine,
        object(),
        config,
        SimpleNamespace(id="C1"),
    )

    monkeypatch.setattr(job_shop, "resolve_tick_action", lambda *args, **kwargs: "active:noop")
    job_shop.reconcile(
        persistence,
        engine,
        object(),
        config,
        SimpleNamespace(id="C1"),
    )


def test_readers_writers_edge_paths(monkeypatch):
    assert (
        readers_writers._active_candidate(
            SimpleNamespace(resource_reservations=lambda: ())
        )
        == "active:noop"
    )

    assert not readers_writers._reconcile_ready_action(
        SimpleNamespace(context=SimpleNamespace(clock=SimpleNamespace(now=object()))),
        object(),
        readers_writers.ReadersWritersConfig(participants=3, readers=2),
        current=SimpleNamespace(state="active"),
        resources=_Resources(),
    )

    assert not readers_writers._reconcile_active_action(
        SimpleNamespace(resource_demands=lambda: (), resource_reservations=lambda: ()),
        object(),
        object(),
        current=SimpleNamespace(state="ready"),
        action="active:writer-0",
        resources=_Resources(),
    )

    assert readers_writers._reconcile_active_action(
        SimpleNamespace(resource_demands=lambda: (), resource_reservations=lambda: ()),
        object(),
        object(),
        current=SimpleNamespace(state="active"),
        action="active:writer-0",
        resources=_Resources(),
    )

    transitioned: list[str] = []
    monkeypatch.setattr(
        readers_writers,
        "transition",
        lambda engine, current, event: transitioned.append(event),
    )
    monkeypatch.setattr(
        readers_writers,
        "resolve_tick_action",
        lambda *args, **kwargs: "ready",
    )
    current = SimpleNamespace(state="active", id="C1")
    readers_writers.reconcile(
        SimpleNamespace(
            entity=lambda *args, **kwargs: current,
            resource_reservations=lambda: (),
            resource_demands=lambda: (),
        ),
        SimpleNamespace(
            resources=_Resources(),
            context=SimpleNamespace(clock=SimpleNamespace(tick=1, now=object())),
        ),
        object(),
        readers_writers.ReadersWritersConfig(participants=3, readers=2),
        SimpleNamespace(id="C1"),
    )
    assert transitioned == []


def test_sleeping_barber_edge_paths(monkeypatch):
    persistence = SimpleNamespace(
        resource_definitions=lambda: (SimpleNamespace(name="barber"),),
        store_definitions=lambda: (SimpleNamespace(name="abandoned"),),
    )
    monkeypatch.setattr(sleeping_barber, "seed_case", lambda *args, **kwargs: "seeded")
    config = sleeping_barber.SleepingBarberConfig(participants=2, waiting_chairs=0)
    assert sleeping_barber.seed(persistence, config) == "seeded"

    assert not sleeping_barber._reconcile_ready_action(
        SimpleNamespace(store_items=lambda: ()),
        SimpleNamespace(context=SimpleNamespace(clock=SimpleNamespace(now=object()))),
        object(),
        config,
        current=SimpleNamespace(state="active"),
        resources=_Resources(),
    )

    sleeping_barber._reconcile_active_action(
        SimpleNamespace(resource_demands=lambda: (), resource_reservations=lambda: ()),
        SimpleNamespace(context=SimpleNamespace(clock=SimpleNamespace(now=object()))),
        object(),
        current=SimpleNamespace(state="ready"),
        action="active:noop",
        resources=_Resources(),
    )


def test_canonical_common_and_other_helpers_cover_noop_edges(monkeypatch):
    persistence = MemoryPersistence()
    DurableStoreManager(persistence).define(StoreDefinition(canonical_common.ACTION_STORE))
    seeded = canonical_common.seed_case(
        persistence,
        name="producer_consumer",
        attributes={},
    )
    assert seeded.entity_type == "canonical_case"

    preseeded = MemoryPersistence()
    DurableStoreManager(preseeded).define(
        StoreDefinition("buffer", kind="fifo", capacity=2)
    )
    producer_consumer.seed(
        preseeded,
        producer_consumer.ProducerConsumerConfig(participants=2, capacity=2),
    )
    transitioned: list[str] = []
    monkeypatch.setattr(
        producer_consumer,
        "transition",
        lambda _engine, _current, event: transitioned.append(event),
    )
    monkeypatch.setattr(
        DurableStoreManager,
        "ensure_selection",
        lambda *_args, **_kwargs: None,
    )
    producer_consumer._reconcile_active(
        preseeded,
        SimpleNamespace(context=SimpleNamespace(clock=SimpleNamespace(now=object()))),
        object(),
        producer_consumer.ProducerConsumerConfig(participants=2, capacity=2),
        current=SimpleNamespace(state="active"),
    )
    assert transitioned == []

    existing_resources = MemoryPersistence()
    with existing_resources.transaction() as uow:
        from sose.core.runtime import ResourceDefinition

        uow.save_resource_definition(ResourceDefinition("reader_slots", 2))
        uow.save_resource_definition(ResourceDefinition("writer_gate", 1))
    readers_writers.seed(
        existing_resources,
        readers_writers.ReadersWritersConfig(participants=3, readers=2),
    )
    assert readers_writers._reconcile_active_action(
        SimpleNamespace(resource_demands=lambda: (), resource_reservations=lambda: ()),
        object(),
        object(),
        current=SimpleNamespace(state="active"),
        action="active:noop",
        resources=_Resources(),
    )

    events: list[str] = []
    monkeypatch.setattr(
        sleeping_barber,
        "transition",
        lambda _engine, _current, event: events.append(event),
    )
    sleeping_barber._reconcile_active_action(
        SimpleNamespace(resource_demands=lambda: (), resource_reservations=lambda: ()),
        SimpleNamespace(context=SimpleNamespace(clock=SimpleNamespace(now=object()))),
        object(),
        current=SimpleNamespace(state="active"),
        action="active:noop",
        resources=_Resources(),
    )
    sleeping_barber._reconcile_active_action(
        SimpleNamespace(resource_demands=lambda: (), resource_reservations=lambda: ()),
        SimpleNamespace(context=SimpleNamespace(clock=SimpleNamespace(now=object()))),
        object(),
        current=SimpleNamespace(state="active"),
        action="active:customer-1",
        resources=_Resources(),
    )
    assert events == ["finish", "finish"]

    single = _Resources({"p0-fork-0": _reservation("p0-fork-0", resource_name="fork-0")})
    dining_philosophers._release_philosopher_if_holding(
        resources=single,
        backend=object(),
        philosopher=0,
        participants=1,
        requested_at=object(),
    )
    assert single.released == ["res-p0-fork-0"]
