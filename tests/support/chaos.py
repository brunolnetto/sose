from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal

from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


CrashPhase = Literal["before_commit", "after_commit"]


class InjectedProcessCrash(BaseException):
    """Synthetic abrupt process death used by the chaos gate."""


class TransactionChaosSQLite(SQLiteIncrementalPersistence):
    """SQLite Engine OLTP with deterministic crash injection per transaction.

    before_commit raises while the underlying SQLite transaction is still
    open; closing the connection models a process dying before COMMIT.
    after_commit raises only after the durable COMMIT completed, modeling a
    process dying before the caller observes success.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        crash_at_transaction: int | None = None,
        crash_phase: CrashPhase | None = None,
    ) -> None:
        if crash_at_transaction is not None and crash_at_transaction < 1:
            raise ValueError("crash_at_transaction must be >= 1")
        if (crash_at_transaction is None) != (crash_phase is None):
            raise ValueError(
                "crash_at_transaction and crash_phase must be supplied together"
            )
        self.transaction_count = 0
        self.crash_at_transaction = crash_at_transaction
        self.crash_phase = crash_phase
        self.crashed = False
        super().__init__(path)

    def _should_crash(self, transaction_index: int, phase: CrashPhase) -> bool:
        return (
            not self.crashed
            and self.crash_at_transaction == transaction_index
            and self.crash_phase == phase
        )

    @contextmanager
    def transaction(self, *, owner_epoch: int | None = None) -> Iterator:
        self.transaction_count += 1
        transaction_index = self.transaction_count

        with super().transaction(owner_epoch=owner_epoch) as uow:
            yield uow
            if self._should_crash(transaction_index, "before_commit"):
                self.crashed = True
                raise InjectedProcessCrash(
                    f"crash before commit at transaction {transaction_index}"
                )

        if self._should_crash(transaction_index, "after_commit"):
            self.crashed = True
            raise InjectedProcessCrash(
                f"crash after commit at transaction {transaction_index}"
            )
