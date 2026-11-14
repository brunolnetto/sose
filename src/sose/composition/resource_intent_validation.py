"""Preconditions of IntentResourceCoordinator, kept in their original order."""
from __future__ import annotations

from datetime import timedelta
from typing import Mapping

from sose.composition.effects import CERTIFIED_INTENTS
from sose.core.resource_identity import ResourcePoolContract


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
