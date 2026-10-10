"""Resource identity is not equivalent to a capacity number or a string label."""
import pytest

from sose.core.resource_identity import (
    ActiveResourceClaim, ResourceAddress, ResourcePoolContract,
)


def test_fungible_shared_pool_preserves_finite_capacity():
    pool = ResourcePoolContract(
        resource_type="authorization_processor", pool_id="global",
        scope="shared", capacity=2,
    )
    pool.validate_active([
        ActiveResourceClaim("order-a", pool.address()),
        ActiveResourceClaim("order-b", pool.address()),
    ])
    with pytest.raises(ValueError, match="capacity exceeded"):
        pool.validate_active([
            ActiveResourceClaim("a", pool.address()),
            ActiveResourceClaim("b", pool.address()),
            ActiveResourceClaim("c", pool.address()),
        ])
    with pytest.raises(ValueError, match="capacity exceeded"):
        pool.validate_active([ActiveResourceClaim("too-large", pool.address(), 3)])
    with pytest.raises(ValueError, match="duplicate resource claim_id"):
        pool.validate_active([
            ActiveResourceClaim("same", pool.address()),
            ActiveResourceClaim("same", pool.address()),
        ])


def test_named_instance_cannot_be_shared_even_with_available_other_instances():
    pool = ResourcePoolContract(
        resource_type="forklift", pool_id="site-a", scope="organization",
        organization_id="manufacturing", capacity=2,
        instance_ids=("forklift-01", "forklift-02"),
    )
    pool.validate_active([
        ActiveResourceClaim("work-a", pool.address(instance_id="forklift-01")),
        ActiveResourceClaim("work-b", pool.address(instance_id="forklift-02")),
    ])
    with pytest.raises(ValueError, match="allocated twice"):
        pool.validate_active([
            ActiveResourceClaim("work-a", pool.address(instance_id="forklift-01")),
            ActiveResourceClaim("work-b", pool.address(instance_id="forklift-01")),
        ])
    with pytest.raises(ValueError, match="name a member"):
        pool.validate_active([ActiveResourceClaim("missing-assignment", pool.address())])
    with pytest.raises(ValueError, match="not a member"):
        pool.address(instance_id="forklift-99")


def test_mismatched_pool_owner_and_scope_fails_closed():
    local = ResourcePoolContract(
        resource_type="operator", pool_id="staff", scope="organization",
        organization_id="o2c", capacity=1,
    )
    foreign = ResourceAddress(
        resource_type="operator", pool_id="staff", scope="organization",
        organization_id="mro",
    )
    with pytest.raises(ValueError, match="different scoped pool"):
        local.validate_active([ActiveResourceClaim("other", foreign)])
    with pytest.raises(ValueError, match="different scoped pool"):
        local.validate_active([
            ActiveResourceClaim("shared", ResourceAddress(
                resource_type="operator", pool_id="staff", scope="shared",
            )),
        ])


def test_scoped_canonical_resource_keys_do_not_alias():
    local_a = ResourceAddress(
        "operator", "staff", "organization", organization_id="factory-a",
    )
    local_b = ResourceAddress(
        "operator", "staff", "organization", organization_id="factory-b",
    )
    shared = ResourceAddress("operator", "staff", "shared")
    first = ResourceAddress(
        "forklift", "site-a", "organization",
        organization_id="factory-a", instance_id="01",
    )
    second = ResourceAddress(
        "forklift", "site-a", "organization",
        organization_id="factory-a", instance_id="02",
    )
    assert len({x.lock_key() for x in (local_a, local_b, shared, first, second)}) == 5
    assert shared.lock_key() == ResourceAddress("operator", "staff", "shared").lock_key()
    # JSON serialization must not alias concatenated fields with separators.
    assert ResourceAddress("x:y", "z", "shared").lock_key() != (
        ResourceAddress("x", "y:z", "shared").lock_key()
    )


@pytest.mark.parametrize("kwargs", [
    {"resource_type": "", "pool_id": "p", "scope": "shared"},
    {"resource_type": "r", "pool_id": "", "scope": "shared"},
    {"resource_type": "r", "pool_id": "p", "scope": "invalid"},
    {"resource_type": "r", "pool_id": "p", "scope": "organization"},
    {"resource_type": "r", "pool_id": "p", "scope": "shared", "organization_id": "org"},
    {"resource_type": " r", "pool_id": "p", "scope": "shared"},
    {"resource_type": "r", "pool_id": "p", "scope": "shared", "instance_id": ""},
])
def test_invalid_resource_address_is_rejected(kwargs):
    with pytest.raises(ValueError):
        ResourceAddress(**kwargs)


@pytest.mark.parametrize("kwargs", [
    {"capacity": 0},
    {"capacity": True},
    {"capacity": 2, "instance_ids": ("a",)},
    {"capacity": 2, "instance_ids": ("a", "a")},
    {"capacity": 2, "instance_ids": ["a", "b"]},
])
def test_invalid_pool_contract_is_rejected(kwargs):
    with pytest.raises((ValueError, TypeError)):
        ResourcePoolContract(
            resource_type="forklift", pool_id="site", scope="shared",
            **kwargs,
        )


def test_fungible_pool_rejects_named_instance_claim():
    pool = ResourcePoolContract(
        resource_type="worker", pool_id="line", scope="shared", capacity=2,
    )
    with pytest.raises(ValueError, match="fungible"):
        pool.validate_active([
            ActiveResourceClaim("job", ResourceAddress(
                "worker", "line", "shared", instance_id="operator-1",
            )),
        ])


def test_active_claim_cannot_divide_physical_instance():
    address = ResourceAddress(
        "forklift", "site", "shared", instance_id="one",
    )
    with pytest.raises(ValueError, match="indivisible"):
        ActiveResourceClaim("job", address, 2)
