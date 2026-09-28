from datetime import datetime, timedelta

from sose.domain.config import DomainConfig

from .simulation import ORIGIN


class EnergyUtilitiesConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 1009

    primary_customer_id: str = "customer-1"
    secondary_customer_id: str = "customer-2"
    include_secondary: bool = True
    quantity_kind: str = "energy"
    unit: str = "kWh"
    auto_meter_reading: bool = True
    meter_reading_quantity: float = 12.5
    demand_response_enabled: bool = True
    demand_response_event_key: str = "dr-1"
    demand_response_start_delay: timedelta = timedelta(hours=1)
    demand_response_duration: timedelta = timedelta(hours=2)
