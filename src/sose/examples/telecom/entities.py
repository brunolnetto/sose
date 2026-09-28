from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class ProductOrder(Entity):
    entity_type: str = "telecom_product_order"


@dataclass(slots=True)
class ServiceOrder(Entity):
    entity_type: str = "telecom_service_order"


@dataclass(slots=True)
class SubscriptionService(Entity):
    entity_type: str = "telecom_subscription_service"


@dataclass(slots=True)
class UsageRecord(Entity):
    entity_type: str = "telecom_usage_record"


@dataclass(slots=True)
class NetworkAlarm(Entity):
    entity_type: str = "telecom_network_alarm"


@dataclass(slots=True)
class TroubleTicket(Entity):
    entity_type: str = "telecom_trouble_ticket"
