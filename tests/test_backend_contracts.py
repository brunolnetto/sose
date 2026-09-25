from datetime import datetime, timezone

from sose.backends import (
    ContainerBackend,
    PreemptiveResourceBackend,
    ResourceBackend,
    StoreBackend,
    TemporalBackend,
)
from sose.backends.simpy import SimPyBackend


def test_simpy_backend_satisfies_all_public_backend_protocols():
    backend = SimPyBackend(origin=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert isinstance(backend, TemporalBackend)
    assert isinstance(backend, ResourceBackend)
    assert isinstance(backend, PreemptiveResourceBackend)
    assert isinstance(backend, StoreBackend)
    assert isinstance(backend, ContainerBackend)


def test_public_backend_surface_exposes_implemented_methods():
    backend = SimPyBackend(origin=datetime(2026, 1, 1, tzinfo=timezone.utc))

    expected = {
        "create_resource",
        "request_resource",
        "release_resource",
        "create_preemptive_resource",
        "request_preemptive_resource",
        "release_preemptive_resource",
        "create_store",
        "create_priority_store",
        "create_filter_store",
        "put_store",
        "get_store",
        "create_container",
        "put_container",
        "get_container",
    }

    assert all(callable(getattr(backend, name, None)) for name in expected)
