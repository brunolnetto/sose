from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from sose.backends.simpy import SimPyBackend
from sose.jobs.runner import SimulationJob
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


def operational_snapshot(persistence) -> dict[str, tuple]:
    """Stable operational truth suitable for deterministic continuation checks."""
    return {
        "job": tuple(
            (s.job_id, s.next_tick, s.run_count, s.status, s.phase)
            for s in persistence.job_states()
        ),
        "resources": tuple(sorted(
            (r.resource_name, r.reservation_id, r.request_id)
            for r in persistence.resource_reservations()
        )),
        "demands": tuple(sorted(
            (d.resource_name, d.request_id)
            for d in persistence.resource_demands()
        )),
        "store_results": tuple(sorted(
            (r.store_name, r.request_id, r.item.item_id)
            for r in persistence.store_get_results()
        )),
    }


def assert_resource_exclusive(persistence, resource_name: str) -> None:
    owners = [
        r for r in persistence.resource_reservations()
        if r.resource_name == resource_name
    ]
    assert len(owners) <= 1, (
        f"{resource_name} has multiple owners: "
        f"{[r.request_id for r in owners]}"
    )


def assert_store_capacity(persistence, store_name: str, capacity: int) -> None:
    items = [i for i in persistence.store_items() if i.store_name == store_name]
    assert len(items) <= capacity, (
        f"{store_name} contains {len(items)} items with capacity {capacity}"
    )


def assert_unique_consumption(persistence, store_name: str) -> None:
    results = [
        r for r in persistence.store_get_results()
        if r.store_name == store_name
    ]
    item_ids = [r.item.item_id for r in results]
    assert len(item_ids) == len(set(item_ids)), (
        f"{store_name} consumed an item more than once"
    )


def assert_precedence(
    completed: Iterable[str],
    edges: Iterable[tuple[str, str]],
) -> None:
    order = {operation: index for index, operation in enumerate(completed)}
    for predecessor, successor in edges:
        if successor in order:
            assert predecessor in order, (
                f"{successor} completed before missing predecessor {predecessor}"
            )
            assert order[predecessor] < order[successor], (
                f"{successor} completed before {predecessor}"
            )


def assert_conservation(*, total: int, buckets: dict[str, int]) -> None:
    observed = sum(buckets.values())
    assert observed == total, (
        f"conservation violated: expected {total}, observed {observed}: {buckets}"
    )


def assert_restart_equivalent(
    *,
    definition,
    directory: Path,
    ticks: int,
    restart_at: Iterable[int],
    job_id: str | None = None,
    snapshot: Callable[[Any], Any] = operational_snapshot,
) -> None:
    """Compare uninterrupted execution with arbitrary fresh-process boundaries."""
    points = tuple(sorted(set(restart_at)))
    assert ticks >= 1
    assert all(0 < point < ticks for point in points)

    identity = job_id or f"{definition.name}-equivalence"

    def backend(origin):
        return SimPyBackend(origin=origin)

    continuous_path = directory / f"{definition.name}-continuous.sqlite3"
    continuous = SQLiteIncrementalPersistence(continuous_path)
    job = SimulationJob(
        job_id=identity,
        definition=definition,
        persistence=continuous,
        backend_factory=backend,
    )
    job.initialize()
    for tick in range(1, ticks + 1):
        job.run_tick(trigger_id=f"{definition.name}:{tick}")
    expected = snapshot(continuous)
    continuous.close()

    restarted_path = directory / f"{definition.name}-restarted.sqlite3"
    persistence = SQLiteIncrementalPersistence(restarted_path)
    job = SimulationJob(
        job_id=identity,
        definition=definition,
        persistence=persistence,
        backend_factory=backend,
    )
    job.initialize()

    for tick in range(1, ticks + 1):
        job.run_tick(trigger_id=f"{definition.name}:{tick}")
        if tick in points:
            persistence.close()
            persistence = SQLiteIncrementalPersistence(restarted_path)
            job = SimulationJob(
                job_id=identity,
                definition=definition,
                persistence=persistence,
                backend_factory=backend,
            )

    actual = snapshot(persistence)
    persistence.close()
    assert actual == expected
