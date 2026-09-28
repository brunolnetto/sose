from __future__ import annotations

from sose.domain.config import DomainCatalog
from sose.examples.mro.definition import definition as mro
from sose.examples.tutorial_job.definition import definition as tutorial_job


def builtin_catalog() -> DomainCatalog:
    catalog = DomainCatalog()
    catalog.register(tutorial_job)
    catalog.register(mro)
    return catalog
