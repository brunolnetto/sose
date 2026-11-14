"""Writer-epoch claims and epoch validation of PostgresPersistence (split from postgres.py)."""

from __future__ import annotations

import psycopg
from psycopg import sql

from .postgres_leases import ScopedWriterLease, StaleWriterError, WriterLease


class PostgresWriterFencingMixin:
    """Namespace-wide and job-scoped writer fencing on the adapter's connection.

    The concrete adapter provides ``transaction``.
    """

    namespace: str
    _connection: psycopg.Connection
    _meta_table: sql.Identifier
    _scoped_writers_table: sql.Identifier
    _correlation_owners_table: sql.Identifier

    def writer_epoch(self) -> int:
        row = self._connection.execute(
            sql.SQL("SELECT owner_epoch FROM {} WHERE singleton = 1").format(self._meta_table)
        ).fetchone()
        if row is None:
            raise RuntimeError("writer metadata disappeared")
        return int(row[0])

    def claim_writer(self, owner_id: str, *, expected_epoch: int) -> WriterLease:
        if not owner_id:
            raise ValueError("owner_id cannot be empty")
        if expected_epoch < 0:
            raise ValueError("expected_epoch must be >= 0")
        with self._connection.transaction():
            self._connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (self.namespace,),
            )
            row = self._connection.execute(
                sql.SQL(
                    """
                    UPDATE {}
                    SET owner_id = %s, owner_epoch = owner_epoch + 1
                    WHERE singleton = 1 AND owner_epoch = %s
                    RETURNING owner_epoch
                    """
                ).format(self._meta_table),
                (owner_id, expected_epoch),
            ).fetchone()
            if row is None:
                raise StaleWriterError(f"writer claim lost: expected epoch {expected_epoch}")
            return WriterLease(owner_id=owner_id, epoch=int(row[0]))

    def claim_writer_and_register(
        self,
        owner_id: str,
        *,
        expected_epoch: int,
        initial_job,
    ) -> WriterLease:
        """Atomically fence the writer AND persist a previously absent job ID.

        A crash after this call may leave a pending job, but never an orphaned
        positive writer epoch with no authoritative domain identity.
        """
        with self._connection.transaction():
            lease = self.claim_writer(owner_id, expected_epoch=expected_epoch)
            with self.transaction(owner_epoch=lease.epoch) as uow:
                prior = uow.get_job_state(initial_job.job_id)
                if prior is not None:
                    if prior.domain_name != initial_job.domain_name:
                        raise ValueError("recovery job identity already belongs to another domain")
                    if prior.config_json != initial_job.config_json:
                        raise ValueError("recovery job's durable organizational binding changed")
                else:
                    uow.save_job_state(initial_job)
            return lease

    def scoped_writer_epoch(self, scope: str) -> int:
        if not scope:
            raise ValueError("writer scope must be nonempty")
        row = self._connection.execute(
            sql.SQL("SELECT owner_epoch FROM {} WHERE scope = %s").format(
                self._scoped_writers_table
            ),
            (scope,),
        ).fetchone()
        return 0 if row is None else int(row[0])

    def claim_scoped_writer(
        self,
        scope: str,
        owner_id: str,
        *,
        expected_epoch: int,
        correlation_id: str | None = None,
    ) -> ScopedWriterLease:
        if not scope or not owner_id:
            raise ValueError("scope and owner must be nonempty")
        if expected_epoch < 0:
            raise ValueError("expected_epoch must be >= 0")
        if correlation_id is not None and not correlation_id:
            raise ValueError("correlation_id must be nonempty")
        with self._connection.transaction():
            # Only workers claiming this same scope serialize. Do not use the
            # namespace-wide writer epoch or lock for independent jobs.
            self._connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (self.namespace, "writer-scope:" + scope),
            )
            if correlation_id is not None:
                self._reserve_recovery_correlation(scope, correlation_id)
            row = self._connection.execute(
                sql.SQL("SELECT owner_epoch FROM {} WHERE scope = %s").format(
                    self._scoped_writers_table
                ),
                (scope,),
            ).fetchone()
            current = 0 if row is None else int(row[0])
            if current != expected_epoch:
                raise StaleWriterError(
                    f"scoped writer claim lost for {scope}: expected {expected_epoch}, "
                    f"current {current}"
                )
            self._connection.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (scope, owner_id, owner_epoch)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (scope) DO UPDATE
                    SET owner_id=EXCLUDED.owner_id, owner_epoch=EXCLUDED.owner_epoch
                    """
                ).format(self._scoped_writers_table),
                (scope, owner_id, current + 1),
            )
            return ScopedWriterLease(owner_id, current + 1, scope)

    def _reserve_recovery_correlation(self, scope: str, correlation_id: str) -> None:
        """Bind ``correlation_id`` to ``scope`` inside the caller's claim transaction."""
        original = self._connection.execute(
            sql.SQL("SELECT correlation_id FROM {} WHERE scope = %s").format(
                self._correlation_owners_table
            ),
            (scope,),
        ).fetchone()
        if original is not None and original[0] != correlation_id:
            raise ValueError("recovery job's durable organizational binding changed")
        self._connection.execute(
            sql.SQL(
                "INSERT INTO {} (correlation_id, scope) VALUES (%s, %s) "
                "ON CONFLICT (correlation_id) DO NOTHING"
            ).format(self._correlation_owners_table),
            (correlation_id, scope),
        )
        owner = self._connection.execute(
            sql.SQL("SELECT scope FROM {} WHERE correlation_id = %s").format(
                self._correlation_owners_table
            ),
            (correlation_id,),
        ).fetchone()
        if owner is None or owner[0] != scope:
            raise ValueError("recovery correlation is already owned by another job")

    def claim_scoped_writer_and_register(
        self,
        scope: str,
        owner_id: str,
        *,
        expected_epoch: int,
        correlation_id: str | None,
        initial_job,
    ) -> ScopedWriterLease:
        """Commit scoped claim/correlation reservation and job ID together."""
        with self._connection.transaction():
            lease = self.claim_scoped_writer(
                scope,
                owner_id,
                expected_epoch=expected_epoch,
                correlation_id=correlation_id,
            )
            with self.transaction(owner_epoch=lease.epoch, writer_scope=scope) as uow:
                prior = uow.get_job_state(initial_job.job_id)
                if prior is not None:
                    if prior.domain_name != initial_job.domain_name:
                        raise ValueError("recovery job identity already belongs to another domain")
                    if prior.config_json != initial_job.config_json:
                        raise ValueError("recovery job's durable organizational binding changed")
                else:
                    uow.save_job_state(initial_job)
            return lease

    def _lock_and_validate_epoch(
        self,
        owner_epoch: int | None,
        *,
        writer_scope: str | None = None,
    ) -> int:
        if writer_scope is not None:
            return self._lock_and_validate_scoped_epoch(owner_epoch, writer_scope)
        self._connection.execute(
            "SELECT pg_advisory_xact_lock_shared(hashtext(%s))",
            (self.namespace,),
        )
        owner_row = self._connection.execute(
            sql.SQL("SELECT owner_epoch FROM {} WHERE singleton = 1").format(self._meta_table)
        ).fetchone()
        current_epoch = -1 if owner_row is None else int(owner_row[0])
        if current_epoch > 0 and owner_epoch is None:
            raise StaleWriterError(
                f"writer fencing is active at epoch {current_epoch}; owner_epoch is required"
            )
        if owner_epoch is not None and current_epoch != owner_epoch:
            raise StaleWriterError(
                f"stale writer epoch {owner_epoch}; current epoch is {current_epoch}"
            )
        return current_epoch

    def _lock_and_validate_scoped_epoch(
        self,
        owner_epoch: int | None,
        writer_scope: str,
    ) -> int:
        if not writer_scope or owner_epoch is None:
            raise ValueError("scoped transactions require scope and writer epoch")
        # A scope's exclusive claim waits for its in-flight writes, but
        # cannot preempt or block transactions from another job scope.
        self._connection.execute(
            "SELECT pg_advisory_xact_lock_shared(hashtext(%s), hashtext(%s))",
            (self.namespace, "writer-scope:" + writer_scope),
        )
        row = self._connection.execute(
            sql.SQL("SELECT owner_epoch FROM {} WHERE scope = %s").format(
                self._scoped_writers_table
            ),
            (writer_scope,),
        ).fetchone()
        current = -1 if row is None else int(row[0])
        if current != owner_epoch:
            raise StaleWriterError(
                f"stale scoped writer epoch {owner_epoch} for {writer_scope}; "
                f"current epoch is {current}"
            )
        return current
