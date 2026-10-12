"""Restartable read-side OD-5a causal boundary reconciler.

One invocation advances at most ONE authoritative boundary phase for one
organization's ALREADY COMMITTED physical DomainEvent. Source scheduling,
SimPy execution, physical leases and local clocks are never reconstructed or
mutated by this adapter.

This is not the normal production SimPy recurring runner or a scientific
OD-5a freeze/confirmation. A caller is responsible for repeated invocation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sose.composition.boundary import (
    BoundaryLease, BoundaryService, StaleBoundaryClaimError,
)
from sose.composition.model import (
    BoundaryDelivery, BoundaryMessage, DeliveryStatus,
)
from sose.core.identity import deterministic_id
from sose.persistence.base import Persistence

from .od5a_boundary import (
    _CONTRACT, _DESTINATION, _SOURCE, _VERSION, _physical_event,
    consume_od5a_physical_completion, publish_od5a_physical_completion,
)
from .od5a_observation import (
    _verified_intent, _verified_observation, _verified_prior_certificate,
    _verify_receipt, apply_od5a_physical_observation,
    certify_od5a_physical_observation,
)


Action = Literal[
    "published", "claimed", "acknowledged", "materialized",
    "certified", "blocked", "idle", "complete",
]
_PROGRESS = frozenset({
    "published", "claimed", "acknowledged", "materialized", "certified",
})
_PHYSICAL_NAMES = frozenset({"od5a.execution.v1", "od5a.coordination.v1"})


@dataclass(frozen=True, slots=True)
class OD5aReconcileStep:
    organization_id: str
    action: Action
    event_id: str | None = None
    boundary_message_id: str | None = None
    effect_id: str | None = None
    retry_at: datetime | None = None

    @property
    def advanced(self) -> bool:
        return self.action in _PROGRESS


@dataclass(frozen=True, slots=True)
class _Plan:
    action: Action
    event_id: str | None = None
    message_id: str | None = None
    effect_id: str | None = None
    lease: BoundaryLease | None = None
    retry_at: datetime | None = None


def _expected_message(event, physical, job_id: str) -> BoundaryMessage:
    return BoundaryMessage.create(
        contract_name=_CONTRACT,
        contract_version=_VERSION,
        source_domain=_SOURCE,
        source_identity=job_id,
        destination_domain=_DESTINATION,
        occurrence_key=event.event_id,
        correlation_id=job_id,
        causation_id=event.event_id,
        causation_kind="event",
        produced_at=event.occurred_at,
        payload=physical,
    )


def _plan_one(
    store: Persistence, *, job_id: str, owner_id: str, now: datetime,
) -> _Plan:
    """Observe only authoritative committed facts, never transient processes."""
    with store.transaction() as uow:
        source = uow.get_entity("od5a_durable_pilot", job_id)
        if source is None:
            raise KeyError(f"unknown OD-5a durable organization: {job_id}")
        # The physical DAG and the scheduler's done bit MUST come from the
        # same authoritative transaction snapshot. Otherwise a real source
        # COMMIT between store.events() and transaction() can make us report
        # false completion while its last physical event was never published.
        candidates = sorted(
            (event for event in uow.events()
             if event.entity_type == "od5a_durable_pilot"
             and event.entity_id == job_id
             and event.name in _PHYSICAL_NAMES),
            key=lambda event: (
                event.tick if event.tick is not None else -1, event.event_id,
            ),
        )
        for candidate in candidates:
            event, physical = _physical_event(
                uow, job_id=job_id, event_id=candidate.event_id,
            )
            if event != candidate:
                raise ValueError("OD-5a committed physical candidate changed during reconciliation")
            expected = _expected_message(event, physical, job_id)
            message = uow.get_boundary_message(expected.message_id)
            if message is None:
                return _Plan("published", event.event_id, expected.message_id)
            if message != expected:
                raise ValueError("OD-5a boundary record contradicts canonical physical cause")

            delivery_id = BoundaryDelivery.pending(expected).delivery_id
            delivery = uow.get_boundary_delivery(delivery_id)
            if delivery is None:
                raise RuntimeError("published OD-5a physical event lacks durable delivery")
            if (
                delivery.delivery_id != delivery_id
                or delivery.message_id != expected.message_id
                or delivery.destination_domain != _DESTINATION
                or delivery.contract_name != _CONTRACT
                or delivery.contract_version != _VERSION
            ):
                raise ValueError("OD-5a delivery is inconsistent with physical source")
            effect_id = deterministic_id("od5a-physical-command", message.message_id)
            base = {
                "event_id": event.event_id,
                "message_id": message.message_id,
                "effect_id": effect_id,
            }
            if delivery.status is DeliveryStatus.PENDING:
                if delivery.attempts != delivery.claim_epoch:
                    raise ValueError("OD-5a pending delivery has inconsistent fencing counters")
                if (
                    uow.get_boundary_consumption(delivery_id) is not None
                    or uow.get_command(effect_id) is not None
                    or uow.get_business_effect(effect_id) is not None
                ):
                    raise ValueError("OD-5a pending delivery has impossible downstream truth")
                if now < event.occurred_at:
                    return _Plan("blocked", **base, retry_at=event.occurred_at)
                return _Plan("claimed", **base)
            if delivery.status is DeliveryStatus.CLAIMED:
                if (
                    not delivery.claim_owner_id
                    or delivery.claim_epoch <= 0
                    or delivery.attempts != delivery.claim_epoch
                    or delivery.claimed_at is None
                    or delivery.lease_expires_at is None
                    or delivery.claimed_at < event.occurred_at
                    or delivery.lease_expires_at <= delivery.claimed_at
                    or uow.get_boundary_consumption(delivery_id) is not None
                    or uow.get_command(effect_id) is not None
                    or uow.get_business_effect(effect_id) is not None
                ):
                    raise ValueError("OD-5a claimed delivery has invalid durable fence")
                if now < delivery.claimed_at:
                    return _Plan("blocked", **base, retry_at=delivery.claimed_at)
                if delivery.lease_expires_at <= now:
                    return _Plan("claimed", **base)
                if delivery.claim_owner_id != owner_id:
                    return _Plan(
                        "blocked", **base, retry_at=delivery.lease_expires_at,
                    )
                return _Plan(
                    "acknowledged", **base,
                    lease=BoundaryLease(
                        delivery_id=delivery.delivery_id,
                        message_id=delivery.message_id,
                        owner_id=delivery.claim_owner_id,
                        epoch=delivery.claim_epoch,
                        lease_expires_at=delivery.lease_expires_at,
                    ),
                )
            if delivery.status is not DeliveryStatus.CONSUMED:
                raise ValueError("unsupported OD-5a physical delivery state")

            # A durable ACK must be a real consumed and fenced delivery, not
            # an arbitrary stored transition or a newly fabricated event.
            _verify_receipt(
                uow, message_id=message.message_id, effect_id=effect_id,
            )
            cmd = uow.get_command(effect_id)
            proof = uow.get_business_effect(effect_id)
            if cmd is not None and proof is not None:
                raise ValueError("OD-5a physical effect has Command and certificate simultaneously")
            if cmd is None and proof is None:
                raise ValueError("OD-5a consumed physical delivery lost its Command/certificate")
            if proof is not None:
                _verified_prior_certificate(
                    uow, effect_id=effect_id, prior=proof,
                )
                continue
            _command, physical_event, _data, attrs, ack_at = _verified_intent(
                uow, effect_id=effect_id,
            )
            if now < ack_at:
                return _Plan("blocked", **base, retry_at=ack_at)
            entity = uow.get_entity("od5a_physical_observation", physical_event.event_id)
            if entity is None:
                return _Plan("materialized", **base)
            _verified_observation(
                uow, event=physical_event, attributes=attrs, ack_at=ack_at,
            )
            if now < entity.updated_at:
                return _Plan("blocked", **base, retry_at=entity.updated_at)
            return _Plan("certified", **base)
        return _Plan("complete" if source.attributes["done"] else "idle")


def reconcile_od5a_boundary_step(
    store: Persistence, *, job_id: str, owner_id: str,
    now: datetime, lease_duration: timedelta,
) -> OD5aReconcileStep:
    """Advance at most one boundary phase in one organization.

    Every nontrivial phase uses the authoritative transaction already owned
    by the underlying service. A killed worker can retry the same input:
    publication, fenced claim, consumed ACK, materialization and certificate
    are independently durable and idempotently recoverable.
    """
    if not job_id or not owner_id:
        raise ValueError("reconciliation requires job and owner identities")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("OD-5a reconciliation requires timezone-aware worker time")
    if lease_duration <= timedelta(0):
        raise ValueError("reconciliation requires positive delivery lease")
    plan = _plan_one(store, job_id=job_id, owner_id=owner_id, now=now)
    if plan.action in {"blocked", "idle", "complete"}:
        return OD5aReconcileStep(
            job_id, plan.action, plan.event_id, plan.message_id,
            plan.effect_id, plan.retry_at,
        )
    if plan.action == "published":
        receipt = publish_od5a_physical_completion(
            store, job_id=job_id, event_id=plan.event_id,
        )
        if receipt.message_id != plan.message_id:
            raise RuntimeError("OD-5a publication did not preserve expected causal identity")
    elif plan.action == "claimed":
        lease = BoundaryService(store).claim_next(
            owner_id=owner_id,
            now=now,
            lease_duration=lease_duration,
            destination_domain=_DESTINATION,
            accepted_contracts=frozenset({(_CONTRACT, _VERSION)}),
            message_id=plan.message_id,
            correlation_id=job_id,
        )
        if lease is None:
            return OD5aReconcileStep(
                job_id, "blocked", plan.event_id, plan.message_id,
                plan.effect_id,
            )
        if lease.message_id != plan.message_id:
            raise RuntimeError("OD-5a claim crossed organization or event identity")
    elif plan.action == "acknowledged":
        try:
            consumption = consume_od5a_physical_completion(
                store, lease=plan.lease, now=now,
            )
        except StaleBoundaryClaimError:
            return OD5aReconcileStep(
                job_id, "blocked", plan.event_id, plan.message_id, plan.effect_id,
            )
        if (
            consumption.message_id != plan.message_id
            or consumption.consumer_effect_id != plan.effect_id
        ):
            raise RuntimeError("OD-5a ACK staged an unexpected downstream Command")
    elif plan.action == "materialized":
        observation = apply_od5a_physical_observation(
            store, effect_id=plan.effect_id, applied_at=now,
        )
        if observation.id != plan.event_id:
            raise RuntimeError("OD-5a observation changed physical event identity")
    elif plan.action == "certified":
        certificate = certify_od5a_physical_observation(
            store, effect_id=plan.effect_id, completed_at=now,
        )
        if (
            certificate.entity_id != plan.event_id
            or certificate.effect_id != plan.effect_id
        ):
            raise RuntimeError("OD-5a certification changed physical event identity")
    else:
        raise RuntimeError(f"unknown OD-5a reconciliation action: {plan.action}")
    return OD5aReconcileStep(
        job_id, plan.action, plan.event_id, plan.message_id, plan.effect_id,
    )


__all__ = ["OD5aReconcileStep", "reconcile_od5a_boundary_step"]
