"""Executable admission of organization contracts against real domain registries (#495).

A syntactically valid OrganizationDefinition is not an executable promise.
These tests deliberately require the real DomainCatalog, process manifests, and
BoundaryConsumerRegistry; unsanctioned edges and StateCharts fail closed.
"""
from dataclasses import replace

import pytest

from sose.composition.boundary import BoundaryConsumerRegistry
from sose.composition.organization import (
    BoundaryBinding,
    OrganizationDefinition,
    ProcessComponent,
    validate_organization_admission,
)
from sose.domain.config import DomainCatalog
from sose.examples.catalog import builtin_catalog
from sose.examples.process_manifest import builtin_process_manifests


def _real_definition(*, include_boundary=False):
    components = (
        ProcessComponent(
            component_id="fulfillment",
            domain="warehouse_fulfillment",
            owner_id="wf:owner-a",
            statechart_types=("FulfillmentOrderChart",),
        ),
    )
    boundaries = ()
    if include_boundary:
        components += (
            ProcessComponent(
                component_id="warehouse",
                domain="warehouse_management",
                owner_id="wm:owner-a",
                statechart_types=("WarehouseSiteChart",),
            ),
        )
        boundaries = (
            BoundaryBinding(
                producer_id="fulfillment",
                consumer_id="warehouse",
                contract_name="warehouse.inventory_reservation_requested",
                contract_version=1,
            ),
        )
    return OrganizationDefinition(
        organization_id="organization:real-registry-test", version=1,
        components=components, boundaries=boundaries,
    )


def _admit(spec, *, manifests=None, consumers=None, catalog=None):
    return validate_organization_admission(
        spec,
        catalog=builtin_catalog() if catalog is None else catalog,
        manifests=(builtin_process_manifests() if manifests is None else manifests),
        consumers=BoundaryConsumerRegistry() if consumers is None else consumers,
    )


def test_real_domain_runtime_accepts_registered_chart_without_mutating_business_truth():
    spec = _real_definition()
    assert _admit(spec) == spec.fingerprint()


def test_real_domain_runtime_rejects_fictitious_chart_class():
    spec = _real_definition()
    component = replace(spec.components[0], statechart_types=("ImaginaryChart",))
    with pytest.raises(ValueError, match="unregistered StateChart"):
        _admit(replace(spec, components=(component,)))


def test_domain_catalog_and_process_manifest_are_both_required():
    spec = _real_definition()
    with pytest.raises(KeyError, match="unknown domain"):
        _admit(spec, catalog=DomainCatalog())
    with pytest.raises(ValueError, match="process manifest"):
        _admit(spec, manifests={})


def test_real_pc6_edge_fails_when_consumer_manifest_has_not_declared_ingress():
    spec = _real_definition(include_boundary=True)
    consumers = BoundaryConsumerRegistry()
    consumers.register(
        destination_domain="warehouse_management",
        contract_name="warehouse.inventory_reservation_requested",
        contract_version=1,
        handler=lambda _message, _uow: "test-effect",
    )
    with pytest.raises(ValueError, match="ingress manifest"):
        _admit(spec, consumers=consumers)


def test_registry_handler_must_be_registered_even_if_manifests_accept_edge():
    spec = _real_definition(include_boundary=True)
    manifests = builtin_process_manifests()
    manifests["warehouse_management"] = replace(
        manifests["warehouse_management"],
        ingress_contracts=frozenset({"warehouse.inventory_reservation_requested.v1"}),
    )
    with pytest.raises(ValueError, match="unregistered consumer contract"):
        _admit(spec, manifests=manifests)


def test_fully_qualified_versioned_edge_can_pass_when_registry_and_both_manifests_agree():
    # Only this test fixture extends the as-yet-unratified WM ingress manifest.
    # Production builtin manifests must continue failing closed until audited.
    spec = _real_definition(include_boundary=True)
    manifests = builtin_process_manifests()
    manifests["warehouse_management"] = replace(
        manifests["warehouse_management"],
        ingress_contracts=frozenset({"warehouse.inventory_reservation_requested.v1"}),
    )
    consumers = BoundaryConsumerRegistry()
    consumers.register(
        destination_domain="warehouse_management",
        contract_name="warehouse.inventory_reservation_requested",
        contract_version=1,
        handler=lambda _message, _uow: "test-effect",
    )
    assert _admit(spec, manifests=manifests, consumers=consumers) == spec.fingerprint()
    with pytest.raises(ValueError, match="egress manifest"):
        _admit(
            replace(spec, boundaries=(replace(spec.boundaries[0], contract_version=2),)),
            manifests=manifests, consumers=consumers,
        )


def test_feedback_cycle_requires_explicit_liveness_contract_and_is_not_admitted():
    from sose.composition.organization import _reject_unadmitted_cycles

    with pytest.raises(ValueError, match="cyclic"):
        _reject_unadmitted_cycles(
            (
                BoundaryBinding("first", "second", "first.done", 1),
                BoundaryBinding("second", "first", "second.done", 1),
            )
        )
    _reject_unadmitted_cycles(
        (BoundaryBinding("first", "second", "first.done", 1),)
    )
