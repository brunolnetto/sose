"""O2C, card-payment and R2R journal intent executors, split from trading_company_customer."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime

from sose.backends.simpy import SimPyBackend
from sose.persistence.memory import MemoryPersistence

from sose.examples.cards_payments import simulation as payments
from sose.examples.order_to_cash import simulation as o2c
from sose.examples.record_to_report import simulation as r2r

from .trading_company_customer_support import (
    _causal_command_scope,
    _dispatch_o2c_event,
    _IntentExecution,
)


def _start_and_fulfill(run: _IntentExecution, engine, order):
    persistence, intent, correlation_id = run.persistence, run.intent, run.correlation_id
    if order.state == "ordered":
        _dispatch_o2c_event(
            persistence,
            engine,
            order_id=order.id,
            event="start_fulfillment",
            correlation_id=correlation_id,
            causation_id=intent.command_id,
        )
        order = persistence.entity("sales_order", order.id)
    if order is not None and order.state in {"fulfilling", "partial_fulfillment"}:
        _dispatch_o2c_event(
            persistence,
            engine,
            order_id=order.id,
            event="fulfill",
            correlation_id=correlation_id,
            causation_id=intent.command_id,
        )
        order = persistence.entity("sales_order", order.id)
    return order


def _ship_and_invoice(run: _IntentExecution, engine, order) -> None:
    persistence, intent, correlation_id = run.persistence, run.intent, run.correlation_id
    if order is not None and order.state == "fulfilled":
        _dispatch_o2c_event(
            persistence,
            engine,
            order_id=order.id,
            event="ship",
            correlation_id=correlation_id,
            causation_id=intent.command_id,
        )
        order = persistence.entity("sales_order", order.id)
    if order is not None and order.state == "shipped":
        _dispatch_o2c_event(
            persistence,
            engine,
            order_id=order.id,
            event="invoice",
            correlation_id=correlation_id,
            causation_id=intent.command_id,
        )


def _execute_complete_external_fulfillment(run: _IntentExecution) -> None:
    persistence, intent, fixtures = run.persistence, run.intent, run.fixtures
    _, engine = o2c.build_runtime(persistence, owner_id=run.organization_id, now=intent.due_at)
    order = persistence.entity("sales_order", fixtures.o2c.order_id)
    if order is None:
        raise RuntimeError("sales order disappeared")
    order = _start_and_fulfill(run, engine, order)
    _ship_and_invoice(run, engine, order)
    o2c.ensure_receivable(persistence, engine, entities=fixtures.o2c)


def _business_resource_guard(persistence: MemoryPersistence, name: str):
    guard_factory = getattr(persistence, "business_resource_guard", None)
    return (
        guard_factory(name)
        if callable(guard_factory) else nullcontext()
    )


def _restore_runtime(run: _IntentExecution, domain) -> tuple[object, SimPyBackend]:
    """Rebuild `domain`'s runtime at the persisted position, then advance to the intent."""
    persistence, intent, organization_id = run.persistence, run.intent, run.organization_id
    position = persistence.simulation_position(owner_id=organization_id)
    restore_at = position.logical_time if position is not None else intent.due_at
    target_at = max(restore_at, intent.due_at)
    _, engine = domain.build_runtime(persistence, owner_id=organization_id, now=restore_at)
    backend = SimPyBackend(origin=restore_at)
    engine.rebuild_backend(backend)
    # Reattach durable resource reservations before any release operation.
    backend.run_until(backend.now)
    if target_at > backend.now:
        backend.run_until(target_at)
    engine.context.clock.now = target_at
    return engine, backend


@dataclass(frozen=True, slots=True)
class _PaymentRecovery:
    """One settlement intent together with the runtime rebuilt to resume it."""

    run: _IntentExecution
    engine: object
    backend: SimPyBackend
    payment_id: str

    def payment_state(self) -> str:
        current = self.run.persistence.entity("card_payment", self.payment_id)
        if current is None:
            raise RuntimeError("composed card payment disappeared")
        return current.state


def _pending_settlement_due_times(
    persistence: MemoryPersistence, payment_id: str,
) -> list[datetime]:
    return [
        scheduled.due_at
        for work in persistence.scheduled_work()
        if (scheduled := persistence.command(work.command_id)) is not None
        and scheduled.entity_type == "card_payment"
        and scheduled.entity_id == payment_id
        and scheduled.name == "settlement_due"
    ]


def _resume_captured_payment(recovery: _PaymentRecovery) -> None:
    persistence, engine, backend = recovery.run.persistence, recovery.engine, recovery.backend
    payment_id = recovery.payment_id
    # Recovery may occur after capture committed but before its
    # durable settlement_due schedule was inserted.
    pending = _pending_settlement_due_times(persistence, payment_id)
    if len(pending) > 1:
        raise RuntimeError("card payment has ambiguous durable settlement schedules")
    if pending:
        due_at = pending[0]
    else:
        current = persistence.entity("card_payment", payment_id)
        due_at = max(
            backend.now,
            (current.updated_at or backend.now) + payments.SETTLEMENT_DELAY,
        )
        command = engine.context.commands.create(
            "settlement_due", target=current, due_at=due_at,
            correlation_id=payments.flow_correlation_id(),
            key=("cards-settlement", payment_id, "due"),
        )
        engine.context.schedules.at(due_at, command=command)
    backend.run_until(max(backend.now, due_at))


def _resume_payment_stages(recovery: _PaymentRecovery) -> None:
    persistence, engine, backend = recovery.run.persistence, recovery.engine, recovery.backend
    fixtures, payment_id = recovery.run.fixtures, recovery.payment_id
    if recovery.payment_state() == "authorization_requested":
        if not payments.reconcile_authorization(
            persistence, engine, backend,
            entities=fixtures.payments, outcome="authorize",
        ):
            raise RuntimeError("composed payment authorization failed")
    if recovery.payment_state() == "authorized":
        settlement_at = payments.reconcile_capture_and_schedule_settlement(
            persistence, engine, backend, entities=fixtures.payments,
        )
        backend.run_until(settlement_at)

    if recovery.payment_state() == "captured":
        _resume_captured_payment(recovery)

    if recovery.payment_state() == "settlement_pending":
        payments.reconcile_settlement(
            persistence, engine, backend,
            entities=fixtures.payments, outcome="success",
        )

    if recovery.payment_state() != "settled":
        raise RuntimeError("composed card payment did not settle")
    # An interruption after the terminal state transition but before
    # withdraw leaves a durable reservation that must still be freed.
    engine.resources.withdraw(backend, f"authorization-processor:{payment_id}")
    engine.resources.withdraw(backend, f"settlement-processor:{payment_id}")


def _execute_settle_customer_payment(run: _IntentExecution) -> None:
    # Domain-scoped PostgreSQL guard protects the multi-transaction card
    # authorization/capture/settlement resource lifecycle from competing
    # workers. Distinct customers share the named processor capacity.
    with _business_resource_guard(run.persistence, "cards_payments.authorization_settlement"):
        # Each transition commits independently, so a worker may restart at
        # authorized, captured, settlement_pending or already settled. Never
        # replay an already-committed capture or rewind the logical position.
        engine, backend = _restore_runtime(run, payments)
        recovery = _PaymentRecovery(
            run=run, engine=engine, backend=backend,
            payment_id=run.fixtures.payments.payment_id,
        )
        with _causal_command_scope(
            engine,
            intent=run.intent,
            correlation_id=run.correlation_id,
        ):
            _resume_payment_stages(recovery)


def _execute_post_journal(run: _IntentExecution) -> None:
    persistence, fixtures = run.persistence, run.fixtures
    # A shared posting processor has capacity one. Coordinate the whole
    # journal statechart (which spans several durable transactions) across
    # independent PostgreSQL workers, rather than letting one falsely
    # interpret temporary resource contention as permanent business failure.
    # Other adapters retain their existing single-worker semantics.
    with _business_resource_guard(persistence, "r2r.posting_processor"):
        # Journal submission and posting are separate committed transitions.
        # Restore the backend at the authoritative position before advancing
        # to the intent due time; rebuilding at a later origin is invalid.
        engine, backend = _restore_runtime(run, r2r)
        with _causal_command_scope(
            engine,
            intent=run.intent,
            correlation_id=run.correlation_id,
        ):
            if not r2r.submit_and_post_journal(
                persistence,
                engine,
                backend,
                entities=fixtures.r2r,
            ):
                raise RuntimeError("R2R journal posting failed")
            posted = persistence.entity("journal_entry", fixtures.r2r.journal_id)
            if posted is None or posted.state != "posted":
                raise RuntimeError("R2R journal effect lacks durable posted state")
