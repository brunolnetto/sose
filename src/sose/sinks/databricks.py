from __future__ import annotations

from collections.abc import Callable

from sose.sinks.model import AnalyticalBatch

from .sql_warehouse import batch_values, event_values, validate_table_name


class DatabricksAnalyticalSink:
    """Databricks SQL/Delta analytical event sink.

    The sink is idempotent by batch_id and event_id. It does not own SOSE
    operational truth; it materializes committed event batches downstream.
    """

    def __init__(
        self,
        connection_factory: Callable[[], object],
        *,
        events_table: str = "sose_events",
        batches_table: str = "sose_batches",
        create_tables: bool = True,
    ) -> None:
        self.connection_factory = connection_factory
        self.events_table = validate_table_name(events_table)
        self.batches_table = validate_table_name(batches_table)
        self.create_tables = create_tables

    def _ensure_tables(self, cursor) -> None:
        if not self.create_tables:
            return
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.events_table} (
                event_id STRING,
                batch_id STRING,
                job_id STRING,
                domain_name STRING,
                config_revision BIGINT,
                logical_tick BIGINT,
                logical_time TIMESTAMP,
                event_name STRING,
                entity_type STRING,
                entity_id STRING,
                occurred_at TIMESTAMP,
                event_tick BIGINT,
                payload_json STRING,
                causation_id STRING,
                correlation_id STRING
            ) USING DELTA
            """
        )
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.batches_table} (
                batch_id STRING,
                job_id STRING,
                domain_name STRING,
                config_revision BIGINT,
                logical_tick BIGINT,
                logical_time TIMESTAMP,
                event_count BIGINT
            ) USING DELTA
            """
        )

    def _batch_exists(self, cursor, batch_id: str) -> bool:
        cursor.execute(
            f"SELECT 1 FROM {self.batches_table} WHERE batch_id = ? LIMIT 1",
            [batch_id],
        )
        return cursor.fetchone() is not None

    def _merge_event(self, cursor, values: tuple[object, ...]) -> None:
        columns = (
            "event_id",
            "batch_id",
            "job_id",
            "domain_name",
            "config_revision",
            "logical_tick",
            "logical_time",
            "event_name",
            "entity_type",
            "entity_id",
            "occurred_at",
            "event_tick",
            "payload_json",
            "causation_id",
            "correlation_id",
        )
        aliases = ", ".join(f"? AS {name}" for name in columns)
        insert_columns = ", ".join(columns)
        insert_values = ", ".join(f"source.{name}" for name in columns)
        cursor.execute(
            f"""
            MERGE INTO {self.events_table} AS target
            USING (SELECT {aliases}) AS source
            ON target.event_id = source.event_id
            WHEN NOT MATCHED THEN
              INSERT ({insert_columns})
              VALUES ({insert_values})
            """,
            list(values),
        )

    def _merge_batch(self, cursor, values: tuple[object, ...]) -> None:
        columns = (
            "batch_id",
            "job_id",
            "domain_name",
            "config_revision",
            "logical_tick",
            "logical_time",
            "event_count",
        )
        aliases = ", ".join(f"? AS {name}" for name in columns)
        insert_columns = ", ".join(columns)
        insert_values = ", ".join(f"source.{name}" for name in columns)
        cursor.execute(
            f"""
            MERGE INTO {self.batches_table} AS target
            USING (SELECT {aliases}) AS source
            ON target.batch_id = source.batch_id
            WHEN NOT MATCHED THEN
              INSERT ({insert_columns})
              VALUES ({insert_values})
            """,
            list(values),
        )

    def publish(self, batch: AnalyticalBatch) -> None:
        connection = self.connection_factory()
        cursor = connection.cursor()
        try:
            self._ensure_tables(cursor)
            if self._batch_exists(cursor, batch.batch_id):
                return
            for event in batch.events:
                self._merge_event(cursor, event_values(batch, event))
            self._merge_batch(cursor, batch_values(batch))
            commit = getattr(connection, "commit", None)
            if callable(commit):
                commit()
        finally:
            close_cursor = getattr(cursor, "close", None)
            if callable(close_cursor):
                close_cursor()
            close_connection = getattr(connection, "close", None)
            if callable(close_connection):
                close_connection()
