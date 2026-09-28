from __future__ import annotations

from sose.domain.config import DomainCatalog
from sose.examples.airports.definition import definition as airports
from sose.examples.construction.definition import definition as construction
from sose.examples.credit_loans.definition import definition as credit_loans
from sose.examples.hospitals.definition import definition as hospitals
from sose.examples.insurance.definition import definition as insurance
from sose.examples.itsm.definition import definition as itsm
from sose.examples.manufacturing.definition import definition as manufacturing
from sose.examples.order_to_cash.definition import definition as order_to_cash
from sose.examples.mro.definition import definition as mro
from sose.examples.tutorial_job.definition import definition as tutorial_job


def builtin_catalog() -> DomainCatalog:
    catalog = DomainCatalog()
    catalog.register(tutorial_job)
    catalog.register(airports)
    catalog.register(construction)
    catalog.register(credit_loans)
    catalog.register(hospitals)
    catalog.register(insurance)
    catalog.register(itsm)
    catalog.register(manufacturing)
    catalog.register(order_to_cash)
    catalog.register(mro)
    return catalog
