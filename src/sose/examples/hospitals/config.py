from datetime import datetime, timedelta
from pydantic import Field
from sose.domain.config import DomainConfig
from .runtime import ORIGIN

class HospitalsConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 252
    acuity: int = Field(default=50, ge=0, le=100)
    service: str = "general-medicine"
    ward_bed_capacity: int = Field(default=1, ge=1)
    icu_bed_capacity: int = Field(default=1, ge=1)
    clinical_team_capacity: int = Field(default=1, ge=1)
    procedure_suite_capacity: int = Field(default=1, ge=1)
    triage_queue_capacity: int = Field(default=100, ge=1)
    auto_progress_patient_flow: bool = True
