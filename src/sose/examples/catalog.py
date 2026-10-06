from __future__ import annotations

from sose.domain.config import DomainCatalog
from sose.examples.canonical.producer_consumer import definition as producer_consumer
from sose.examples.canonical.sleeping_barber import definition as sleeping_barber
from sose.examples.canonical.dining_philosophers import definition as dining_philosophers
from sose.examples.canonical.readers_writers import definition as readers_writers
from sose.examples.canonical.job_shop import definition as job_shop
from sose.examples.subscription_saas.definition import definition as subscription_saas
from sose.examples.telecom.definition import definition as telecom
from sose.examples.transit.definition import definition as transit
from sose.examples.warehouse_fulfillment.definition import definition as warehouse_fulfillment
from sose.examples.warehouse_management.definition import definition as warehouse_management
from sose.examples.aviation.definition import definition as aviation
from sose.examples.cards_payments.definition import definition as cards_payments
from sose.examples.energy_utilities.definition import definition as energy_utilities
from sose.examples.field_service.definition import definition as field_service
from sose.examples.hospitality.definition import definition as hospitality
from sose.examples.logistics.definition import definition as logistics
from sose.examples.p2p.definition import definition as p2p
from sose.examples.record_to_report.definition import definition as record_to_report
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
    catalog.register(producer_consumer)
    catalog.register(sleeping_barber)
    catalog.register(dining_philosophers)
    catalog.register(readers_writers)
    catalog.register(job_shop)
    catalog.register(tutorial_job)
    catalog.register(subscription_saas)
    catalog.register(telecom)
    catalog.register(transit)
    catalog.register(warehouse_fulfillment)
    catalog.register(warehouse_management)
    catalog.register(aviation)
    catalog.register(cards_payments)
    catalog.register(energy_utilities)
    catalog.register(field_service)
    catalog.register(hospitality)
    catalog.register(logistics)
    catalog.register(p2p)
    catalog.register(record_to_report)
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
