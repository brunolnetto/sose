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
    OperationalConfigReference,
    ProcessComponent,
    validate_organization_admission,
)
from sose.domain.config import DomainCatalog
from sose.examples.catalog import builtin_catalog
from sose.examples.process_manifest import ProcessEvidence, builtin_process_manifests


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
        source_domain="warehouse_fulfillment",
    )
    with pytest.raises(ValueError, match="ingress manifest"):
        _admit(spec, consumers=consumers)


def _hypothetically_audited_wm_ingress():
    """Local admission fixture; DOES NOT upgrade the built-in WM manifest."""
    manifests = builtin_process_manifests()
    original = manifests["warehouse_management"]
    provenance = dict(original.evidence_sources)
    provenance[ProcessEvidence.INGRESS_CONTRACTS] = (
        "src/sose/composition/trading_company_customer.py",
        "tests/integration/sose/composition/test_pc6_two_real_customer_paths.py",
    )
    manifests["warehouse_management"] = replace(
        original,
        evidence=original.evidence | {ProcessEvidence.INGRESS_CONTRACTS},
        ingress_contracts=frozenset({"warehouse.inventory_reservation_requested.v1"}),
        evidence_sources=provenance,
        contract_provenance=(
            ("ingress", "warehouse.inventory_reservation_requested.v1",
             ("src/sose/composition/trading_company_customer.py",
              "tests/integration/sose/composition/test_pc6_two_real_customer_paths.py")),
        ),
    )
    return manifests


def test_registry_handler_must_be_registered_even_if_manifests_accept_edge():
    spec = _real_definition(include_boundary=True)
    manifests = _hypothetically_audited_wm_ingress()
    with pytest.raises(ValueError, match="unregistered consumer contract"):
        _admit(spec, manifests=manifests)


def test_fully_qualified_versioned_edge_can_pass_when_registry_and_both_manifests_agree():
    # Only this test fixture extends the as-yet-unratified WM ingress manifest.
    # Production builtin manifests must continue failing closed until audited.
    spec = _real_definition(include_boundary=True)
    manifests = _hypothetically_audited_wm_ingress()
    consumers = BoundaryConsumerRegistry()
    consumers.register(
        destination_domain="warehouse_management",
        contract_name="warehouse.inventory_reservation_requested",
        contract_version=1,
        handler=lambda _message, _uow: "test-effect",
        source_domain="warehouse_fulfillment",
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



def test_process_version_is_matched_against_static_domain_registration():
    spec = _real_definition()
    revised = replace(spec.components[0], process_version=2)
    with pytest.raises(ValueError, match="unsupported process_version"):
        _admit(replace(spec, components=(revised,)))


def test_runtime_admission_never_executes_config_or_builder():
    spec = _real_definition()
    catalog = builtin_catalog()
    original = catalog.get("warehouse_fulfillment")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("admission executed an impure domain callback")

    from dataclasses import replace as copy_definition
    custom = DomainCatalog()
    custom.register(copy_definition(
        original, build_runtime=forbidden, seed=forbidden,
    ))
    assert _admit(spec, catalog=custom) == spec.fingerprint()


def test_version_specific_provenance_cannot_be_inherited_from_v1():
    spec = _real_definition(include_boundary=True)
    manifests = _hypothetically_audited_wm_ingress()
    wf = manifests["warehouse_fulfillment"]
    wm = manifests["warehouse_management"]
    # Claim v2 as a string and as aggregate ProcessEvidence, but do NOT supply
    # version-specific provenance: this must fail before handler resolution.
    manifests["warehouse_fulfillment"] = replace(
        wf, egress_contracts=wf.egress_contracts |
        {"warehouse.inventory_reservation_requested.v2"},
    )
    manifests["warehouse_management"] = replace(
        wm, ingress_contracts=wm.ingress_contracts |
        {"warehouse.inventory_reservation_requested.v2"},
    )
    v2 = replace(spec, boundaries=(replace(spec.boundaries[0], contract_version=2),))
    consumers = BoundaryConsumerRegistry()
    consumers.register(
        source_domain="warehouse_fulfillment",
        destination_domain="warehouse_management",
        contract_name="warehouse.inventory_reservation_requested",
        contract_version=2,
        handler=lambda _message, _uow: "v2-effect",
    )
    with pytest.raises(ValueError, match="egress manifest"):
        _admit(v2, manifests=manifests, consumers=consumers)


def test_producer_must_match_explicit_consumer_handler_source():
    spec = _real_definition(include_boundary=True)
    consumers = BoundaryConsumerRegistry()
    consumers.register(
        source_domain="cards_payments",
        destination_domain="warehouse_management",
        contract_name="warehouse.inventory_reservation_requested",
        contract_version=1,
        handler=lambda _message, _uow: "wrong-source-effect",
    )
    with pytest.raises(ValueError, match="unregistered consumer contract"):
        _admit(spec, manifests=_hypothetically_audited_wm_ingress(), consumers=consumers)



def test_complete_pc6_manifest_provenance_covers_every_declared_contract_version():
    manifest = builtin_process_manifests()["warehouse_fulfillment"]
    for direction in ("ingress", "egress"):
        names = getattr(manifest, f"{direction}_contracts")
        asserted_names = {
            name for declared_direction, name, _ in manifest.contract_provenance
            if declared_direction == direction
        }
        assert asserted_names == names
        for name in names:
            paths = manifest.contract_sources(direction, name)
            assert paths
            assert all(path.startswith(("src/", "tests/")) for path in paths)


def test_registry_dispatches_distinct_producers_to_distinct_handlers():
    from dataclasses import replace
    from datetime import datetime, timezone
    from sose.composition.boundary import BoundaryMessage, UnsupportedBoundaryContractError

    registry = BoundaryConsumerRegistry()

    def card_payment_handler(_message, _uow):
        return "card-settlement"

    def purchase_receipt_handler(_message, _uow):
        return "purchase-accrual"

    for source, handler in (
        ("cards_payments", card_payment_handler),
        ("procure_to_pay", purchase_receipt_handler),
    ):
        registry.register(
            source_domain=source, destination_domain="record_to_report",
            contract_name="accounting.entry_requested", contract_version=1,
            handler=handler,
        )

    assert registry.contracts_for_source(
        source_domain="cards_payments", destination_domain="record_to_report",
    ) == frozenset({("accounting.entry_requested", 1)})
    msg = BoundaryMessage.create(
        contract_name="accounting.entry_requested", contract_version=1,
        source_domain="cards_payments", source_identity="card:1",
        destination_domain="record_to_report", occurrence_key="1",
        correlation_id="corr:1", causation_id=None,
        produced_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        payload={"kind": "card"},
    )
    assert registry.resolve(msg) is card_payment_handler
    assert registry.resolve(replace(msg, source_domain="procure_to_pay")) is purchase_receipt_handler
    with pytest.raises(UnsupportedBoundaryContractError):
        registry.resolve(replace(msg, source_domain="unknown_producer"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            source_domain="cards_payments", destination_domain="record_to_report",
            contract_name="accounting.entry_requested", contract_version=1,
            handler=card_payment_handler,
        )


def test_unqualified_consumer_does_not_become_admission_evidence():
    registry = BoundaryConsumerRegistry()
    registry.register(
        destination_domain="record_to_report",
        contract_name="accounting.entry_requested", contract_version=1,
        handler=lambda *_: "generic",
    )
    assert registry.contracts_for("record_to_report") == frozenset({
        ("accounting.entry_requested", 1),
    })
    assert not registry.contracts_for_source(
        source_domain="cards_payments", destination_domain="record_to_report",
    )



@pytest.mark.parametrize("invalid_version", [1.0, 2.5, True, False, "1", None])
def test_domain_definition_rejects_noninteger_process_versions(invalid_version):
    original = builtin_catalog().get("warehouse_fulfillment")
    with pytest.raises(ValueError, match="process_version must be a positive integer"):
        replace(original, process_version=invalid_version)


def test_domain_definition_accepts_positive_integer_process_version():
    original = builtin_catalog().get("warehouse_fulfillment")
    assert replace(original, process_version=2).process_version == 2


def test_v2_domain_and_component_cannot_reuse_v1_manifest_evidence():
    # The real builtin ProcessManifest is a v1-only claim. Merely changing
    # both domain and component versions must not upgrade that evidence.
    original_domain = builtin_catalog().get("warehouse_fulfillment")
    custom_catalog = DomainCatalog()
    custom_catalog.register(replace(original_domain, process_version=2))
    spec = _real_definition()
    versioned = replace(
        spec, components=(replace(spec.components[0], process_version=2),),
    )
    with pytest.raises(ValueError, match="version-specific process manifest"):
        _admit(versioned, catalog=custom_catalog)


@pytest.mark.parametrize("not_handler", [None, 17, "not-callable", object()])
def test_registry_rejects_noncallable_handler_before_admission(not_handler):
    registry = BoundaryConsumerRegistry()
    with pytest.raises(ValueError, match="callable"):
        registry.register(
            source_domain="warehouse_fulfillment",
            destination_domain="warehouse_management",
            contract_name="warehouse.inventory_reservation_requested",
            contract_version=1,
            handler=not_handler,
        )
    assert not registry.contracts_for_source(
        source_domain="warehouse_fulfillment",
        destination_domain="warehouse_management",
    )
    with pytest.raises(ValueError, match="unregistered consumer contract"):
        _admit(
            _real_definition(include_boundary=True),
            manifests=_hypothetically_audited_wm_ingress(),
            consumers=registry,
        )


def test_registry_accepts_executable_handler_for_admission():
    registry = BoundaryConsumerRegistry()
    registry.register(
        source_domain="warehouse_fulfillment",
        destination_domain="warehouse_management",
        contract_name="warehouse.inventory_reservation_requested",
        contract_version=1,
        handler=lambda _message, _uow: "certified-effect",
    )
    spec = _real_definition(include_boundary=True)
    assert _admit(
        spec, manifests=_hypothetically_audited_wm_ingress(),
        consumers=registry,
    ) == spec.fingerprint()



@pytest.mark.parametrize("invalid_version", [1.0, 2.5, True, False, "1", None])
def test_consumer_registry_rejects_noninteger_contract_versions(invalid_version):
    registry = BoundaryConsumerRegistry()
    with pytest.raises(ValueError, match="contract_version must be a positive integer"):
        registry.register(
            source_domain="warehouse_fulfillment",
            destination_domain="warehouse_management",
            contract_name="warehouse.inventory_reservation_requested",
            contract_version=invalid_version,
            handler=lambda _message, _uow: "never-authorized",
        )
    assert registry.contracts_for_source(
        source_domain="warehouse_fulfillment",
        destination_domain="warehouse_management",
    ) == frozenset()
    with pytest.raises(ValueError, match="unregistered consumer contract"):
        _admit(
            _real_definition(include_boundary=True),
            manifests=_hypothetically_audited_wm_ingress(),
            consumers=registry,
        )


def test_consumer_registry_accepts_exact_integer_contract_version():
    registry = BoundaryConsumerRegistry()
    registry.register(
        source_domain="warehouse_fulfillment",
        destination_domain="warehouse_management",
        contract_name="warehouse.inventory_reservation_requested",
        contract_version=1,
        handler=lambda _message, _uow: "authorized",
    )
    assert registry.contracts_for_source(
        source_domain="warehouse_fulfillment",
        destination_domain="warehouse_management",
    ) == frozenset({("warehouse.inventory_reservation_requested", 1)})



def test_organization_admission_rejects_ambiguous_same_domain_boundary_ownership():
    # A domain-only handler cannot identify which independently owned component
    # is the actual producer/consumer. Do not certify two owners from one key.
    spec = _real_definition(include_boundary=True)
    second = replace(
        spec.components[0],
        component_id="fulfillment-secondary",
        owner_id="wf:owner-b",
    )
    multi_owner = replace(spec, components=(*spec.components, second))
    registry = BoundaryConsumerRegistry()
    registry.register(
        source_domain="warehouse_fulfillment",
        destination_domain="warehouse_management",
        contract_name="warehouse.inventory_reservation_requested",
        contract_version=1,
        handler=lambda _message, _uow: "first-owner-only",
    )
    with pytest.raises(ValueError, match="owner-qualified boundary"):
        _admit(
            multi_owner,
            manifests=_hypothetically_audited_wm_ingress(),
            consumers=registry,
        )


def test_organization_allows_disconnected_same_domain_components_without_boundaries():
    # Static catalog introspection does not route any cross-process effect.
    spec = _real_definition()
    another = replace(
        spec.components[0],
        component_id="fulfillment-secondary",
        owner_id="wf:owner-b",
    )
    independent = replace(spec, components=(*spec.components, another))
    assert _admit(independent) == independent.fingerprint()


@pytest.mark.parametrize("kind", ("calendar", "policy", "cost_model"))
def test_static_registry_admission_rejects_self_attested_operational_config(kind):
    """A caller-provided hash does not install/attest a live policy or calendar."""
    spec = _real_definition()
    config = OperationalConfigReference(
        component_id="fulfillment", kind=kind,
        reference_id="declared:ref", reference_version=1,
        source_sha256="a" * 64, config_epoch=0,
    )
    declared_only = replace(spec, operational_configs=(config,))
    # Fingerprint is a structural identifier, NOT proof of installed runtime
    # semantics or conformance of costs, physical availability or governance.
    assert declared_only.fingerprint() != spec.fingerprint()
    with pytest.raises(ValueError, match="require audited runtime admission"):
        _admit(declared_only)
    assert _admit(spec) == spec.fingerprint()
