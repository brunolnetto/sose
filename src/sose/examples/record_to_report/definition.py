from datetime import datetime

from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence

from .config import RecordToReportConfig
from .simulation import (
    build_runtime,
    post_adjustment,
    reconcile_close,
    reconcile_item,
    schedule_close,
    seed_reference,
    submit_and_post_journal,
)


def _build(
    persistence: Persistence,
    config: RecordToReportConfig,
    now: datetime,
    tick: int,
):
    return build_runtime(
        persistence,
        now=now,
        tick=tick,
        step=config.tick_step,
        random_seed=config.random_seed,
    )


def _seed(persistence: Persistence, config: RecordToReportConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        amount=config.amount,
        currency=config.currency,
    )


def _reconcile_tick(persistence, engine, backend, config, entities) -> None:
    journal = persistence.entity("journal_entry", entities.journal_id)
    item = persistence.entity(
        "reconciliation_item",
        entities.reconciliation_id,
    )
    task = persistence.entity("close_task", entities.close_task_id)
    if journal is None or item is None or task is None:
        raise RuntimeError("R2R reference entities were not persisted")

    if journal.state != "posted":
        submit_and_post_journal(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        return

    if item.state in {"pending", "reconciling"}:
        reconcile_item(
            persistence,
            engine,
            backend,
            entities=entities,
            outcome=config.reconciliation_outcome,
        )
        return

    if item.state in {"unmatched", "adjustment_required"}:
        post_adjustment(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        return

    if item.state in {"matched", "reconciled"} and task.state == "pending":
        schedule_close(
            persistence,
            engine,
            backend,
            task_id=entities.close_task_id,
            period_id=entities.period_id,
            delay=config.close_delay,
        )
        return

    if task.state == "in_progress":
        reconcile_close(
            persistence,
            engine,
            backend,
            entities=entities,
        )


definition = DomainDefinition(
    name="record_to_report",
    description="Record-to-Report reference domain.",
    config_model=RecordToReportConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
    runtime_mutable_fields=frozenset(["tick_step","random_seed","reconciliation_outcome","close_delay"]),
)
