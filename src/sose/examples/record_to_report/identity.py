from __future__ import annotations

from sose.core.identity import deterministic_id


def flow_correlation_id(period_id: str) -> str:
    return deterministic_id("r2r-flow", period_id)


def adjustment_id(reconciliation_id: str) -> str:
    return deterministic_id(
        "entity",
        "accounting_adjustment",
        "r2r-reference",
        reconciliation_id,
        "adjustment-1",
    )


def close_task_id(period_id: str, ordinal: int) -> str:
    return deterministic_id(
        "entity",
        "close_task",
        "r2r-reference",
        period_id,
        "close-task",
        ordinal,
    )
