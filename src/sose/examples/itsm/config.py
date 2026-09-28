from datetime import datetime, timedelta
from pydantic import Field
from sose.domain.config import DomainConfig
from .simulation import ORIGIN

class ITSMConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 210
    severity: int = Field(default=50, ge=0, le=100)
    service: str = "payments-api"
    support_agent_capacity: int = Field(default=1, ge=1)
    escalation_manager_capacity: int = Field(default=1, ge=1)
    incident_queue_capacity: int = Field(default=100, ge=1)
    sla_delay: timedelta = Field(default=timedelta(hours=4), gt=timedelta(0))
