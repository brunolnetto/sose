"""The bounded work loop of one PC6 recovery occurrence (split from recovery.py)."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sose.composition import trading_company_customer as customer
from sose.composition.boundary import BoundaryService
from sose.composition.model import BoundaryMessage
from sose.composition.recovery_boundary import (
    _LOGISTICS_CONTRACT,
    _O2C_CONTRACT,
    _PAYMENTS_CONTRACT,
    _R2R_CONTRACT,
    _WM_CONTRACT,
)
from sose.composition.recovery_charging import charge_committed_work
from sose.composition.resource_intents import IntentResourceCoordinator
from sose.core.resource_reservations import TemporalReservation
from sose.persistence.base import Persistence

if TYPE_CHECKING:
    from sose.composition.recovery import TradingCustomerRecoveryRunner


# Claim order of the destination domains; the first available lease wins.
_CLAIM_ORDER = (
    ("warehouse_management", _WM_CONTRACT),
    ("logistics", _LOGISTICS_CONTRACT),
    ("order_to_cash", _O2C_CONTRACT),
    ("cards_payments", _PAYMENTS_CONTRACT),
    ("record_to_report", _R2R_CONTRACT),
)


@dataclass(slots=True)
class _Pass:
    """Values captured once for the whole bounded pass."""

    runner: TradingCustomerRecoveryRunner
    store: Persistence
    service: BoundaryService
    resource_intents: IntentResourceCoordinator | None
    now: datetime
    logical_now: datetime | None
    active_trigger: str | None = None
    # A deferred effect stays durable, but must not monopolize this tick.
    deferred_this_tick: set[str] = field(default_factory=set)


def _reconcile_certified(tick: _Pass, actions: int) -> int:
    """Charge recovered physical releases; return the new action count."""
    runner = tick.runner
    # A recovered release is a committed physical/causal action, not
    # free extra work. Otherwise a resumed occurrence may emit the
    # NEXT boundary effect one slot earlier than uninterrupted replay.
    owned_organizations = runner.resource_organizations
    if runner.correlation_id is not None:
        owned_organizations = {
            runner.correlation_id: runner.resource_organizations[runner.correlation_id],
        }
    return actions + tick.resource_intents.reconcile_certified(
        tick.store, owned_organizations,
        max_completed=runner.max_actions - actions,
        trigger_id=tick.active_trigger,
    )


def _owned_pending(tick: _Pass) -> list[tuple[BoundaryMessage, str]]:
    runner = tick.runner
    return [
        pair for pair in runner._pending_effects(tick.store)
        if pair[1] not in tick.deferred_this_tick
        and (runner.correlation_id is None or pair[0].correlation_id == runner.correlation_id)
    ]


def _admit(tick: _Pass, source: BoundaryMessage, effect_id: str, command: Any) -> bool:
    runner, store, active_trigger = tick.runner, tick.store, tick.active_trigger
    demand_due = command.due_at
    if (
        command.name == "composition.deliver_shipment"
        and command.name in runner.resource_policies
        and store.entity("shipment", command.entity_id) is not None
    ):
        demand_due = customer.pickup_resource_due_at(
            store, command,
            organization_id=runner.resource_organizations[source.correlation_id],
        )
    return tick.resource_intents.admit(
        effect_id=effect_id,
        intent_name=command.name,
        organization_id=runner.resource_organizations[source.correlation_id],
        causation_id=source.message_id,
        correlation_id=source.correlation_id,
        due_at=demand_due,
        now=tick.logical_now if tick.logical_now is not None else tick.now,
        **({"trigger_id": active_trigger} if active_trigger is not None else {}),
    )


def _enroll_effect(tick: _Pass, effect_id: str) -> None:
    runner, active_trigger = tick.runner, tick.active_trigger
    with tick.store.transaction() as uow:
        job = uow.get_job_state(runner.job_id)
        if job is None or job.active_trigger_id != active_trigger:
            raise RuntimeError("recovery effect lost its active trigger")
        if effect_id not in job.active_effect_ids:
            uow.save_job_state(replace(
                job,
                active_effect_ids=(*job.active_effect_ids, effect_id),
            ))


def _execute(
    tick: _Pass, source: BoundaryMessage, effect_id: str,
    *, organization_id: str, pickup: TemporalReservation | None,
) -> None:
    runner, store = tick.runner, tick.store
    if pickup is not None:
        if runner.resource_persistence is not None:
            runner._execute_pending(
                store, source, effect_id, organization_id=organization_id,
                authoritative_pickup=pickup,
                resource_persistence=runner.resource_persistence,
            )
        else:
            runner._execute_pending(
                store, source, effect_id, organization_id=organization_id,
                authoritative_pickup=pickup,
            )
    else:
        runner._execute_pending(
            store, source, effect_id, organization_id=organization_id,
        )


def _is_unmapped_enrolled_effect(tick: _Pass, command: Any) -> bool:
    runner = tick.runner
    return (
        tick.active_trigger is not None
        and (runner.resource_policies is None
             or command.name not in runner.resource_policies)
    )


def _booked_pickup(tick: _Pass, effect_id: str, command: Any) -> TemporalReservation | None:
    resource_intents = tick.resource_intents
    return (
        resource_intents.booking_for(effect_id, command.name)
        if resource_intents is not None
        and command.name == "composition.deliver_shipment"
        and command.name in tick.runner.resource_policies
        else None
    )


def _run_pending_effect(tick: _Pass, source: BoundaryMessage, effect_id: str) -> int:
    """Replay one accepted effect; return 1 when executed, 0 when deferred."""
    runner, store, resource_intents = tick.runner, tick.store, tick.resource_intents
    organization_id = runner._organization_for(source.correlation_id)
    command = store.command(effect_id)
    if command is None:
        raise RuntimeError("accepted effect disappeared before execution")
    if resource_intents is not None:
        admitted = _admit(tick, source, effect_id, command)
        if not admitted:
            tick.deferred_this_tick.add(effect_id)
            return 0
    # Every non-physical business action is durably enrolled in
    # the owning trigger BEFORE the real domain transition. On
    # restart its immutable certificate consumes the same slot.
    # Resource-mapped actions already have an authoritative
    # admission-to-trigger link in the physical PostgreSQL ledger.
    if _is_unmapped_enrolled_effect(tick, command):
        _enroll_effect(tick, effect_id)
    pickup = _booked_pickup(tick, effect_id, command)
    try:
        _execute(tick, source, effect_id, organization_id=organization_id, pickup=pickup)
    except customer.DurableResourceWait:
        # Physical capacity contention is waiting, not failure.
        # The immutable Command stays durable for another slot.
        tick.deferred_this_tick.add(effect_id)
        return 0
    if resource_intents is not None:
        resource_intents.complete(effect_id)
    return 1


def _emitted_egress(tick: _Pass, reconcile: Callable[..., Any], actions: int) -> int:
    """Reconstruct one egress kind; count the messages that did not exist before."""
    runner, store = tick.runner, tick.store
    with store.transaction() as uow:
        before_ids = {
            delivery.message_id for delivery in uow.boundary_deliveries()
        }
    reconstructed = reconcile(
        store, organization_owners=runner._egress_organization_owners(),
        max_new_messages=runner.max_actions - actions,
        recovery_job_id=runner.job_id if tick.active_trigger is not None else None,
        correlation_id=runner.correlation_id,
    )
    return sum(message.message_id not in before_ids for message in reconstructed)


def _reconstruct_egress(tick: _Pass, actions: int) -> tuple[int, bool, bool]:
    """Return ``(actions, budget_exhausted, emitted_any)`` after the four egress kinds."""
    runner = tick.runner
    emitted = _emitted_egress(tick, customer.reconcile_shipped_fulfillment_egress, actions)
    if emitted:
        actions += emitted
        # Do not silently exceed the configured work budget.
        if actions >= runner.max_actions:
            return actions, True, True
    emitted_logistics = _emitted_egress(
        tick, customer.reconcile_completed_logistics_egress, actions,
    )
    actions += emitted_logistics
    if actions >= runner.max_actions:
        return actions, True, True
    emitted_o2c = _emitted_egress(tick, customer.reconcile_invoiced_o2c_egress, actions)
    actions += emitted_o2c
    if actions >= runner.max_actions:
        return actions, True, True
    emitted_payment = _emitted_egress(
        tick, customer.reconcile_settled_payment_egress, actions,
    )
    actions += emitted_payment
    if actions >= runner.max_actions:
        return actions, True, True
    idle = not emitted and not emitted_logistics and not emitted_o2c and not emitted_payment
    return actions, False, not idle


def _claim_next(tick: _Pass) -> Any:
    runner = tick.runner
    lease = None
    for destination_domain, contract in _CLAIM_ORDER:
        lease = tick.service.claim_next(
            owner_id=runner.owner_id,
            now=tick.now,
            lease_duration=runner.lease_duration,
            destination_domain=destination_domain,
            accepted_contracts=frozenset({contract}),
            correlation_id=runner.correlation_id,
        )
        if lease is not None:
            break
    return lease


def _enroll_ack(tick: _Pass, lease: Any) -> None:
    runner, active_trigger = tick.runner, tick.active_trigger
    with tick.store.transaction() as uow:
        job = uow.get_job_state(runner.job_id)
        if job is None or job.active_trigger_id != active_trigger:
            raise RuntimeError("recovery ACK lost its active trigger")
        if lease.message_id not in job.active_boundary_message_ids:
            uow.save_job_state(replace(
                job,
                active_boundary_message_ids=(
                    *job.active_boundary_message_ids, lease.message_id,
                ),
            ))


def _consume_claimed(tick: _Pass, lease: Any) -> None:
    runner = tick.runner
    with tick.store.transaction() as uow:
        message = uow.get_boundary_message(lease.message_id)
    if message is None:
        raise RuntimeError("claimed WM boundary message disappeared")
    # Enroll before the ACK transaction, not afterward: an actual
    # OS-kill between COMMIT and the next Python statement must leave
    # sufficient evidence to charge this exact scheduled occurrence.
    # An uncommitted ACK is not charged and can be reclaimed safely.
    if tick.active_trigger is not None:
        _enroll_ack(tick, lease)
    tick.service.consume(
        lease=lease, registry=runner._registry_for(
            message, organization_id=runner._organization_for(message.correlation_id),
        ), now=tick.now,
    )


def _work_loop(tick: _Pass, actions: int) -> int:
    runner = tick.runner
    for _ in range(runner.max_actions):
        if actions >= runner.max_actions:
            break
        pending = _owned_pending(tick)
        if pending:
            source, effect_id = pending[0]
            actions += _run_pending_effect(tick, source, effect_id)
            continue
        actions, exhausted, emitted_any = _reconstruct_egress(tick, actions)
        if exhausted:
            break
        lease = _claim_next(tick)
        if lease is None:
            if not emitted_any:
                break
            continue
        _consume_claimed(tick, lease)
        actions += 1
    return actions


def run_bounded_pass(
    runner: TradingCustomerRecoveryRunner, store: Persistence,
    service: BoundaryService, resource_intents: IntentResourceCoordinator | None,
    *, now: datetime, logical_now: datetime | None,
) -> int:
    """Charge committed work, reconcile certified releases, then spend the budget."""
    tick = _Pass(runner, store, service, resource_intents, now, logical_now)
    actions, tick.active_trigger = charge_committed_work(runner, store)
    # Reconcile a crash after immutable domain certification but before
    # release of its authoritative temporal reservation.
    if resource_intents is not None and actions < runner.max_actions:
        actions = _reconcile_certified(tick, actions)
    if actions >= runner.max_actions:
        return actions
    return _work_loop(tick, actions)
