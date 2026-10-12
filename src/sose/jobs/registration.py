"""Shared PostgreSQL registration fence for SimulationJobState identities.

All state creators must protect the *same* job ID before inspecting or
materializing domain-specific state. Locks are scoped to one job, not a global
simulation clock or organization-wide serializer. Non-Postgres backends keep
their native transaction semantics.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sose.persistence.base import Persistence


@contextmanager
def job_registration_guard(store: Persistence, job_id: str) -> Iterator[None]:
    if not job_id:
        raise ValueError("job registration requires a nonempty identity")
    guard = getattr(store, "business_resource_guard", None)
    if callable(guard):
        with guard("sose:job-registration:" + job_id):
            yield
    else:
        yield
