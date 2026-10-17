"""RED contract for #477: external physical authority must gate native source commits.

This deliberately fails on main until the OD-5a recurring executor can bind
native instance grants to the existing authoritative shared PostgreSQL temporal
resource ledger. It is NOT a substitute for PostgreSQL interleaving tests.
"""
from __future__ import annotations

import inspect

from sose.organizational.od5a_recurring import (
    execute_od5a_claimed_tick,
    run_od5a_recurring_tick,
)


def test_recurring_source_exposes_explicit_shared_pool_binding() -> None:
    """An independent organization's shared resource cannot be invented from job_id.

    An explicit binding is mandatory so legacy local execution remains separate,
    while workers A/B genuinely address the same pool/instance identities.
    """
    for entrypoint in (execute_od5a_claimed_tick, run_od5a_recurring_tick):
        parameters = inspect.signature(entrypoint).parameters
        assert "shared_pool_binding" in parameters, (
            f"{entrypoint.__name__} lacks explicit shared_pool_binding; "
            "currently native grants are only job_id/execution:N owner-local"
        )


def test_shared_binding_carries_one_authoritative_pool_and_fenced_lease_identity() -> None:
    """Implementation acceptance: binding and grant must preserve:

    (scope=shared, pool_id, named physical instance, operational owner,
     reservation_id, source causal event and exact temporal interval).

    This is intentionally a RED import until the production authority adapter
    is available; no duplicate local resource ledger may certify shared use.
    """
    from sose.organizational import od5a_recurring

    assert hasattr(od5a_recurring, "SharedPhysicalPoolBinding"), (
        "missing explicit shared pool identity/version/ownership contract"
    )
    assert hasattr(od5a_recurring, "SharedPhysicalGrantAuthority"), (
        "missing authoritative cross-owner PostgreSQL grant/fencing adapter"
    )
