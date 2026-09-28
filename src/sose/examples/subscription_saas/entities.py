from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class Subscription(Entity):
    entity_type: str = "saas_subscription"


@dataclass(slots=True)
class Entitlement(Entity):
    entity_type: str = "saas_entitlement"


@dataclass(slots=True)
class ChangeRequest(Entity):
    entity_type: str = "saas_change_request"


@dataclass(slots=True)
class SubscriptionOccurrence(Entity):
    entity_type: str = "saas_subscription_occurrence"
