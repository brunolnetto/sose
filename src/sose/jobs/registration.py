"""Cross-adapter, per-job registration fence for SimulationJobState identities.

The guard surrounds initial ownership checks and claims, not business
execution or a global organizational clock. PostgreSQL uses scoped advisory
locks. File-backed SQLite uses a stable process-shared OS file lock because
multiple BEGIN IMMEDIATE transactions cannot protect the gap between commits.
Never unlink live lock files: doing so would admit two different inodes/locks
for the same job.
"""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
from typing import Iterator

from sose.persistence.base import Persistence


@contextmanager
def _sqlite_job_guard(store: Persistence, job_id: str) -> Iterator[None]:
    from sose.persistence.sqlite import SQLitePersistence
    from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence

    if not isinstance(store, (SQLitePersistence, SQLiteIncrementalPersistence)):
        yield
        return
    if store.path == ":memory:":
        # Separate memory databases cannot race over the same durable job.
        yield
        return

    namespace = getattr(store, "namespace", "sose")
    digest = sha256(f"{namespace}\x00{job_id}".encode("utf-8")).hexdigest()
    database = Path(store.path).resolve()
    lock_path = database.with_name(database.name + ".registration." + digest + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        if os.name == "posix":
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        elif os.name == "nt":
            import errno
            import msvcrt
            from time import sleep

            # LK_LOCK retries only ~10 times, then raises, even when another
            # healthy process simply needs longer to finish registration.
            # Retry only lock-contention errors; fail closed on real IO errors.
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if (getattr(exc, "winerror", None) not in {33, 36}
                        and exc.errno not in {errno.EACCES, errno.EAGAIN}):
                        raise
                    sleep(0.05)
        else:
            raise RuntimeError("no OS-process-safe SQLite job registration lock")
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == "posix":
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


@contextmanager
def job_registration_guard(store: Persistence, job_id: str) -> Iterator[None]:
    if not job_id:
        raise ValueError("job registration requires a nonempty identity")
    guard = getattr(store, "business_resource_guard", None)
    if callable(guard):
        with guard("sose:job-registration:" + job_id):
            yield
    else:
        with _sqlite_job_guard(store, job_id):
            yield
