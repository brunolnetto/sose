"""Preconditions and consistency checks of IntentResourceCoordinator, in their original order."""
from __future__ import annotations

from datetime import timedelta
from typing import Mapping

from sose.composition.effects import CERTIFIED_INTENTS
from sose.core.resource_identity import ResourcePoolContract
from sose.core.resource_reservations import ResourceConflictError, _aware


def validate_coordinator_arguments(
    persistence,
    policies: Mapping[str, ResourcePoolContract],
    *,
    slot_duration: timedelta,
    retry_delay: timedelta,
    owner_epoch: int | None,
    writer_scope: str | None,
) -> None:
    if slot_duration <= timedelta(0) or retry_delay <= timedelta(0):
        raise ValueError("resource slot and retry duration must be positive")
    if not policies:
        raise ValueError("resource policy cannot be empty")
    if not hasattr(persistence, "temporal_resources") or not hasattr(persistence, "_connection"):
        raise TypeError("authoritative intent coordination requires PostgreSQL")
    if writer_scope is not None and (not writer_scope or owner_epoch is None):
        raise ValueError('scoped resource admission requires a valid writer epoch')
    validate_intent_policies(dict(policies))


def validate_intent_policies(policies: dict[str, ResourcePoolContract]) -> None:
    if any(not k or not isinstance(v, ResourcePoolContract) for k, v in policies.items()):
        raise ValueError("invalid named domain resource policy")
    if not set(policies).issubset(CERTIFIED_INTENTS):
        raise ValueError("resource-mapped intents require immutable business certification")


def _require_same_reservation(existing, organization_id: str, pool, causation_id) -> None:
    if existing.owner_id != organization_id:
        raise ResourceConflictError("effect's organizational ownership changed")
    if existing.address != pool.address():
        raise ResourceConflictError("effect's resource pool identity changed")
    if existing.causation_id != causation_id:
        raise ResourceConflictError("resource intent's causal predecessor changed")
    if existing.status not in ("reserved", "released"):
        raise ResourceConflictError("reserved intent was failed or preempted")


def _require_link_certificate(certificate, organizations, organization_id: str,
                              causation_id, correlation_id) -> None:
    if certificate is None:
        raise ResourceConflictError(
            "linked resource effect lacks both durable Command and business certificate"
        )
    if certificate.correlation_id != correlation_id:
        raise ResourceConflictError("resource reservation certificate correlation mismatch")
    if certificate.boundary_message_id != causation_id:
        raise ResourceConflictError("resource reservation certificate causation mismatch")
    if organizations.get(certificate.correlation_id) != organization_id:
        raise ResourceConflictError("resource reservation certificate organization mismatch")


def _require_admission_identity(effect_id, organization_id, trigger_id, now, due_at) -> None:
    if not effect_id or not organization_id:
        raise ValueError("effect_id and organization_id must be nonempty")
    if trigger_id is not None and not trigger_id:
        raise ValueError("admitted trigger identity must be nonempty")
    _aware(now, "now")
    _aware(due_at, "due_at")
