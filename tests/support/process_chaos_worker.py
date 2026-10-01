from __future__ import annotations

import argparse
from contextlib import contextmanager
from pathlib import Path
import time
from typing import Iterator

from sose.backends.simpy import SimPyBackend
from sose.examples.catalog import builtin_catalog
from sose.jobs.runner import SimulationJob
from sose.persistence.postgres import PostgresPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


def _checkpoint_coverage() -> None:
    try:
        from coverage import Coverage
    except ImportError:
        return
    current = Coverage.current()
    if current is not None:
        current.save()


def _publish_marker(marker: Path, payload: str) -> None:
    temporary = marker.with_name(f".{marker.name}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(marker)


def _pause(
    marker: Path,
    continue_file: Path | None,
    *,
    payload: str = "ready",
) -> None:
    _checkpoint_coverage()
    if not marker.exists():
        _publish_marker(marker, payload)
    if continue_file is None:
        while True:
            time.sleep(60)
    deadline = time.monotonic() + 30
    while not continue_file.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"chaos worker continue signal timed out: {continue_file}")
        time.sleep(0.01)


class PausingSQLite(SQLiteIncrementalPersistence):
    def __init__(self, *args, pause_at: int, phase: str, marker: Path, **kwargs):
        self._pause_at = pause_at
        self._phase = phase
        self._marker = marker
        self._transaction_count = 0
        super().__init__(*args, **kwargs)

    @contextmanager
    def transaction(self, *, owner_epoch: int | None = None) -> Iterator:
        self._transaction_count += 1
        index = self._transaction_count
        with super().transaction(owner_epoch=owner_epoch) as uow:
            yield uow
            if index == self._pause_at and self._phase == "before_commit":
                _pause(self._marker, None)
        if index == self._pause_at and self._phase == "after_commit":
            _pause(self._marker, None)


class PausingPostgres(PostgresPersistence):
    def __init__(
        self,
        *args,
        pause_at: int,
        phase: str,
        marker: Path,
        continue_file: Path,
        **kwargs,
    ):
        self._pause_at = pause_at
        self._phase = phase
        self._marker = marker
        self._continue_file = continue_file
        self._transaction_count = 0
        super().__init__(*args, **kwargs)

    @contextmanager
    def transaction(self, *, owner_epoch: int | None = None) -> Iterator:
        self._transaction_count += 1
        index = self._transaction_count
        with super().transaction(owner_epoch=owner_epoch) as uow:
            yield uow
            if index == self._pause_at and self._phase == "before_commit":
                pid = self._connection.execute("SELECT pg_backend_pid()").fetchone()[0]
                _pause(
                    self._marker,
                    self._continue_file,
                    payload=str(pid),
                )
        if index == self._pause_at and self._phase == "after_commit":
            pid = self._connection.execute("SELECT pg_backend_pid()").fetchone()[0]
            _pause(
                self._marker,
                self._continue_file,
                payload=str(pid),
            )


def _run(persistence, name: str) -> None:
    definition = builtin_catalog().get(name)
    job = SimulationJob(
        job_id=f"{name}-process-chaos",
        definition=definition,
        persistence=persistence,
        backend_factory=lambda origin: SimPyBackend(origin=origin),
    )
    job.initialize()
    for tick in range(1, 4):
        job.run_tick(trigger_id=f"{name}:process-chaos:{tick}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("sqlite", "postgres"), required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--pause-at", type=int, required=True)
    parser.add_argument("--phase", choices=("before_commit", "after_commit"), required=True)
    parser.add_argument("--marker", type=Path, required=True)
    parser.add_argument("--continue-file", type=Path)
    parser.add_argument("--sqlite-path", type=Path)
    parser.add_argument("--dsn")
    parser.add_argument("--namespace")
    args = parser.parse_args()

    args.marker.parent.mkdir(parents=True, exist_ok=True)
    if args.backend == "sqlite":
        if args.sqlite_path is None:
            raise ValueError("--sqlite-path is required for sqlite")
        persistence = PausingSQLite(
            args.sqlite_path,
            pause_at=args.pause_at,
            phase=args.phase,
            marker=args.marker,
        )
    else:
        if not args.dsn or not args.namespace or args.continue_file is None:
            raise ValueError("--dsn, --namespace and --continue-file are required for postgres")
        persistence = PausingPostgres(
            args.dsn,
            namespace=args.namespace,
            pause_at=args.pause_at,
            phase=args.phase,
            marker=args.marker,
            continue_file=args.continue_file,
        )

    try:
        _run(persistence, args.name)
    finally:
        persistence.close()


if __name__ == "__main__":
    main()
