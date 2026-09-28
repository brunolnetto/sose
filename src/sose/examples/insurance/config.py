from typing import Literal
from datetime import datetime, timedelta
from pydantic import Field
from sose.domain.config import DomainConfig
from .simulation import ORIGIN

class InsuranceConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 511
    severity: int = Field(default=50, ge=0, le=100)
    amount: float = Field(default=5000.0, gt=0)
    claim_type: str = "property"
    currency: str = "USD"
    document_deadline: timedelta = Field(default=timedelta(hours=4), gt=timedelta(0))
    auto_satisfy_documents: bool = True
    assessment_outcome: Literal["approve", "reject", "fraud"] = "approve"
    payment_delay: timedelta = Field(default=timedelta(hours=2), gt=timedelta(0))
    partial_payment: bool = False
    fraud_confirmed: bool = False
