from datetime import datetime, timedelta
from pydantic import Field
from sose.domain.config import DomainConfig
from .simulation import ORIGIN

class CreditLoansConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 842
    principal: float = Field(default=1200.0, gt=0)
    installment_count: int = Field(default=3, ge=1)
    approve_application: bool = True
    auto_pay_due_installments: bool = True
    first_due_delay: timedelta = Field(default=timedelta(days=30), gt=timedelta(0))
    installment_interval: timedelta = Field(default=timedelta(days=30), gt=timedelta(0))
    overdue_grace: timedelta = Field(default=timedelta(days=5), gt=timedelta(0))
