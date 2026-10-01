from __future__ import annotations

import argparse
from contextlib import contextmanager
from pathlib import Path
import time
from typing import Iterator

from sose.backends.simpy import SimPyBackend
from sose.examples.catalog import builtin_catalog
from sose.jobs.persistent import PersistentJobRunner
from sose.jobs.runner import SimulationJob
from sose.persistence.postgres import PostgresPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


def _pause(marker: Path, continue_file: Path | None, epoch: int) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(epoch), encoding="utf-8")
    if continue_file is None:
        while True:
            time.sleep(60)
    deadline = time.monotonic() + 30
    while not continue_file.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"continue signal timed out: {continue_file}")
        time.sleep(0.01)


class _PausingMixin:
    pause_at: int | None
    pause_phase: str | None
    marker: Path | None
    continue_file: Path | None
    fenced_transactions: int

    def _configure_pause(
        self,
        *,
        pause_at: int | None,
        pause_phase: str | None,
        marker: Path | None,
        continue_file: Path | None,
    ) -> None:
        self.pause_at = pause_at
        self.pause_phase = pause_phase
        self.marker = marker
        self.continue_file = continue_file
        self.fenced_transactions = 0

    def _matches(self, index: int, phase: str, owner_epoch: int | None) -> bool:
        return (
            owner_epoch is not None
            and self.pause_at == index
            and self.pause_phase == phase
            and self.marker is not None
        )


class PausingSQLite(_PausingMixin, SQLiteIncrementalPersistence):
    def __init__(self, path, **pause):
        self._configure_pause(**pause)
        super().__init__(path)

    @contextmanager
    def transaction(self, *, owner_epoch: int | None = None) -> Iterator:
        if owner_epoch is not None:
            self.fenced_transactions += 1
        index = self.fenced_transactions
        with super().transaction(owner_epoch=owner_epoch) as uow:
            yield uow
            if self._matches(index, "before_commit", owner_epoch):
                _pause(self.marker, self.continue_file, owner_epoch)
        if self._matches(index, "after_commit", owner_epoch):
            _pause(self.marker, self.continue_file, owner_epoch)


class PausingPostgres(_PausingMixin, PostgresPersistence):
    def __init__(self, dsn, *, namespace, **pause):
        self._configure_pause(**pause)
        super().__init__(dsn, namespace=namespace)

    @contextmanager
    def transaction(self, *, owner_epoch: int | None = None) -> Iterator:
        if owner_epoch is not None:
            self.fenced_transactions += 1
        index = self.fenced_transactions
        with super().transaction(owner_epoch=owner_epoch) as uow:
            yield uow
            if self._matches(index, "before_commit", owner_epoch):
                _pause(self.marker, self.continue_file, owner_epoch)
        if self._matches(index, "after_commit", owner_epoch):
            _pause(self.marker, self.continue_file, owner_epoch)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("sqlite", "postgres"), required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--trigger-id", required=True)
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--pause-at", type=int)
    parser.add_argument("--pause-phase", choices=("before_commit", "after_commit"))
    parser.add_argument("--marker", type=Path)
    parser.add_argument("--continue-file", type=Path)
    parser.add_argument("--result-file", type=Path)
    parser.add_argument("--sqlite-path", type=Path)
    parser.add_argument("--dsn")
    parser.add_argument("--namespace")
    args = parser.parse_args()

    pause = {
        "pause_at": args.pause_at,
        "pause_phase": args.pause_phase,
        "marker": args.marker,
        "continue_file": args.continue_file,
    }
    if args.backend == "sqlite":
        if args.sqlite_path is None:
            raise ValueError("--sqlite-path is required")
        persistence = PausingSQLite(args.sqlite_path, **pause)
    else:
        if not args.dsn or not args.namespace:
            raise ValueError("--dsn and --namespace are required")
        persistence = PausingPostgres(args.dsn, namespace=args.namespace, **pause)

    definition = builtin_catalog().get(args.name)
    job = SimulationJob(
        job_id=f"{args.name}-fencing-chaos",
        definition=definition,
        persistence=persistence,
        backend_factory=lambda origin: SimPyBackend(origin=origin),
    )
    runner = PersistentJobRunner(job, owner_id=args.owner_id)
    try:
        result = runner.run_tick(
            trigger_id=args.trigger_id,
            recover=args.recover,
        )
        if args.result_file is not None:
            args.result_file.write_text(
                f"{result.owner_id}:{result.epoch}:{result.result.logical_tick}",
                encoding="utf-8",
            )
    finally:
        persistence.close()


if __name__ == "__main__":
    main()
