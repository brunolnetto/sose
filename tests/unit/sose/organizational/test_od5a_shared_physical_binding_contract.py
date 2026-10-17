"""Adversarial #477 shared-pool identity and fail-closed binding contracts."""
from __future__ import annotations

from dataclasses import replace

import pytest

from sose.core.resource_identity import ResourcePoolContract
from sose.domain.entity import Entity
from sose.organizational.od5a_recurring import (
    SharedPhysicalPoolBinding, _assert_shared_binding,
)


def _pool(*, scope="shared", instances=("unit-1", "unit-2")):
    return ResourcePoolContract(
        resource_type="execution_technician", pool_id="site-a",
        scope=scope, organization_id="org:a" if scope == "organization" else None,
        capacity=2, instance_ids=instances,
    )


def _binding(*, pool=None, mapping=(("execution:0", "unit-1"),)):
    return SharedPhysicalPoolBinding(
        pool=_pool() if pool is None else pool, execution_instances=mapping,
    )


def test_shared_binding_exact_physical_identity_is_immutable_and_stable():
    binding = _binding(mapping=(
        ("execution:0", "unit-1"), ("execution:1", "unit-2"),
    ))
    assert binding.pool.scope == "shared"
    assert binding.physical_instance("execution:0") == "unit-1"
    assert binding.physical_instance("execution:1") == "unit-2"
    assert binding.physical_instance("coordination:0") is None
    assert binding.physical_instance("execution:missing") is None
    assert binding.fingerprint() == _binding(mapping=binding.execution_instances).fingerprint()
    assert binding.fingerprint() != _binding().fingerprint()
    with pytest.raises(AttributeError):
        binding.authority_version = 2


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"pool": object()}, "pool contract"),
        ({"pool": _pool(scope="organization")}, "enumerated shared"),
        ({"pool": _pool(instances=())}, "enumerated shared"),
        ({"authority_version": 2}, "version"),
        ({"authority_version": True}, "version"),
        ({"execution_instances": []}, "mapping"),
        ({"execution_instances": ()}, "mapping"),
        ({"execution_instances": (("execution:0",),)}, "mapping"),
        ({"execution_instances": (["execution:0", "unit-1"],)}, "mapping"),
        ({"execution_instances": (("coordination:0", "unit-1"),)}, "execution"),
        ({"execution_instances": (("execution:0", "unit-1"), ("execution:0", "unit-2"))}, "alias"),
        ({"execution_instances": (("execution:0", "unit-1"), ("execution:1", "unit-1"))}, "alias"),
        ({"execution_instances": (("execution:0", "foreign-instance"),)}, "not a member"),
    ],
)
def test_shared_binding_rejects_ambiguous_or_spoofed_physical_address(overrides, message):
    with pytest.raises((ValueError, TypeError), match=message):
        SharedPhysicalPoolBinding(
            pool=overrides.get("pool", _pool()),
            execution_instances=overrides.get(
                "execution_instances", (("execution:0", "unit-1"),),
            ),
            authority_version=overrides.get("authority_version", 1),
        )


def test_persisted_shared_binding_fails_closed_for_missing_or_changed_authority():
    binding = _binding()
    source = Entity(
        id="org:a", entity_type="od5a_durable_pilot", state="running",
        attributes={"shared_pool_binding": binding.fingerprint()},
    )
    _assert_shared_binding(source, binding)
    with pytest.raises(ValueError, match="explicit immutable binding"):
        _assert_shared_binding(source, None)
    with pytest.raises(ValueError, match="differs"):
        _assert_shared_binding(source, _binding(mapping=(("execution:0", "unit-2"),)))
    with pytest.raises(TypeError, match="SharedPhysicalPoolBinding"):
        _assert_shared_binding(source, object())

    unbound = replace(source, attributes={})
    _assert_shared_binding(unbound, None)
    with pytest.raises(ValueError, match="differs"):
        _assert_shared_binding(unbound, binding)
