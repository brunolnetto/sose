"""Cross-worker PostgreSQL concurrency must reflect *resource identity*, not labels.

Advisory guards represent multi-transaction exclusivity. Pool capacity > 1
requires separate transactional reservation accounting; this gate intentionally
does not claim that a single advisory mutex implements capacity N.
"""
from threading import Event, Thread
from uuid import uuid4
import os

import pytest

from sose.core.resource_identity import ResourcePoolContract
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")


@pytest.mark.parametrize(
    ("case", "expected_to_block"),
    [
        ("same-shared-pool", True),
        ("different-organization-pools", False),
        ("same-physical-instance", True),
        ("different-physical-instances", False),
    ],
)
def test_pg_typed_resource_guards_match_real_contention(case, expected_to_block):
    assert DSN is not None
    namespace = "resource_scope_" + uuid4().hex[:12]
    shared = ResourcePoolContract(
        resource_type="posting_processor", pool_id="shared-office",
        scope="shared", capacity=1,
    )
    local_a = ResourcePoolContract(
        resource_type="posting_processor", pool_id="office",
        scope="organization", organization_id="organization-a", capacity=1,
    )
    local_b = ResourcePoolContract(
        resource_type="posting_processor", pool_id="office",
        scope="organization", organization_id="organization-b", capacity=1,
    )
    physical = ResourcePoolContract(
        resource_type="forklift", pool_id="warehouse",
        scope="shared", capacity=2, instance_ids=("unit-1", "unit-2"),
    )
    addresses = {
        "same-shared-pool": (shared.address(), shared.address()),
        "different-organization-pools": (local_a.address(), local_b.address()),
        "same-physical-instance": (
            physical.address(instance_id="unit-1"),
            physical.address(instance_id="unit-1"),
        ),
        "different-physical-instances": (
            physical.address(instance_id="unit-1"),
            physical.address(instance_id="unit-2"),
        ),
    }
    first_identity, second_identity = addresses[case]
    entered_owner = Event()
    release_owner = Event()
    attempted_second = Event()
    entered_second = Event()
    errors = []

    with (
        PostgresPersistence(DSN, namespace=namespace) as owner,
        PostgresPersistence(DSN, namespace=namespace) as contender,
    ):
        def run_owner():
            try:
                with owner.business_resource_guard(first_identity):
                    entered_owner.set()
                    if not release_owner.wait(10):
                        raise TimeoutError("owner release not signaled")
            except BaseException as exc:
                errors.append(exc)

        def run_contender():
            try:
                attempted_second.set()
                with contender.business_resource_guard(second_identity):
                    entered_second.set()
            except BaseException as exc:
                errors.append(exc)

        a = Thread(target=run_owner)
        b = Thread(target=run_contender)
        a.start()
        assert entered_owner.wait(10), "owner did not acquire resource"
        b.start()
        assert attempted_second.wait(10), "contender did not attempt resource"
        try:
            if expected_to_block:
                assert not entered_second.wait(0.3), (
                    "unrelated claims on one exclusive resource overlapped"
                )
            else:
                assert entered_second.wait(5), (
                    "independent physical instances/organizations serialized"
                )
        finally:
            release_owner.set()
            a.join(timeout=10)
            b.join(timeout=10)
        assert not a.is_alive() and not b.is_alive()
        assert entered_second.is_set()
        assert errors == []


def test_pg_legacy_resource_string_remains_a_valid_guard():
    assert DSN is not None
    namespace = "legacy_scope_" + uuid4().hex[:12]
    with PostgresPersistence(DSN, namespace=namespace) as store:
        with store.business_resource_guard("r2r.posting_processor"):
            assert store.persisted_record_count() == 0
        with pytest.raises(ValueError, match="resource_name"):
            with store.business_resource_guard(""):
                pass
