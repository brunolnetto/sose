"""Contract falsifiers for a versioned organization of independently owned StateCharts.

A catalog is not a runnable organization; execution and projections are #496.
"""
from dataclasses import replace

import pytest

from sose.composition.organization import (
    BoundaryBinding,
    OrganizationDefinition,
    OperationalConfigReference,
    PhysicalPoolRef,
    ProcessComponent,
    ScheduledJob,
)


def organization():
    return OrganizationDefinition(
        organization_id="tenant-a:org-a",
        version=1,
        components=(
            ProcessComponent(
                component_id="sales", domain="order_to_cash", owner_id="o2c-owner",
                statechart_types=("SalesOrderChart", "PaymentChart"),
            ),
            ProcessComponent(
                component_id="maintenance", domain="mro", owner_id="mro-owner",
                statechart_types=("WorkOrderChart", "PartDemandChart"),
            ),
        ),
        boundaries=(
            BoundaryBinding(
                producer_id="sales", consumer_id="maintenance",
                contract_name="maintenance.requested", contract_version=1,
            ),
        ),
        jobs=(
            ScheduledJob(job_id="job:order", component_id="sales", trigger_namespace="order"),
            ScheduledJob(
                job_id="job:maintenance", component_id="maintenance",
                trigger_namespace="maintenance",
            ),
        ),
        physical_pools=(
            PhysicalPoolRef(
                pool_id="staff:technicians", owner_id="resource-ledger",
                consumer_ids=("sales", "maintenance"),
            ),
        ),
    )


def test_valid_organization_describes_components_without_global_lock_or_clock():
    spec = organization()
    assert spec.organization_id == "tenant-a:org-a"
    assert spec.version == 1
    assert spec.config_epoch == 0
    assert len(spec.components) == 2
    assert spec.components[1].statechart_types == ("WorkOrderChart", "PartDemandChart")
    assert {j.trigger_namespace for j in spec.jobs} == {"order", "maintenance"}
    assert spec.physical_pools[0].consumer_ids == ("sales", "maintenance")
    assert not hasattr(spec, "global_clock")
    assert not hasattr(spec, "authoritative_entity_state")


def test_semantically_unordered_catalog_entries_have_one_stable_fingerprint():
    original = organization()
    reordered = replace(
        original,
        components=tuple(reversed((
            original.components[0],
            replace(
                original.components[1],
                statechart_types=tuple(reversed(original.components[1].statechart_types)),
            ),
        ))),
        jobs=tuple(reversed(original.jobs)),
        physical_pools=(replace(
            original.physical_pools[0],
            consumer_ids=tuple(reversed(original.physical_pools[0].consumer_ids)),
        ),),
    )
    assert original.fingerprint() == reordered.fingerprint()


def test_definition_fingerprint_is_stable_and_sensitive_to_semantic_configuration():
    spec = organization()
    assert len(spec.fingerprint()) == 64
    assert spec.fingerprint() == organization().fingerprint()
    assert spec.fingerprint() != replace(spec, config_epoch=1).fingerprint()
    assert spec.fingerprint() != replace(
        spec, boundaries=(
            replace(spec.boundaries[0], contract_version=2),
        ),
    ).fingerprint()


@pytest.mark.parametrize("field,value", [
    ("organization_id", ""),
    ("organization_id", "  "),
    ("version", 0),
    ("config_epoch", -1),
    ("components", ()),
    ("components", (organization().components[0], organization().components[0])),
    ("components", (
        organization().components[0],
        replace(organization().components[1], component_id="sales"),
    )),
    ("components", (
        organization().components[0],
        replace(
            organization().components[1],
            domain="order_to_cash", owner_id="o2c-owner",
        ),
    )),
])
def test_rejects_invalid_organization_or_ambiguous_component_ownership(field, value):
    with pytest.raises(ValueError):
        replace(organization(), **{field: value})


@pytest.mark.parametrize("component", [
    ProcessComponent(component_id="a", domain="mro", owner_id="ops",
                     statechart_types=("WorkOrderChart",)),
])
def test_process_component_is_explicitly_versioned(component):
    assert component.process_version == 1
    with pytest.raises(ValueError):
        replace(component, process_version=0)
    with pytest.raises(ValueError):
        replace(component, statechart_types=("",))
    with pytest.raises(ValueError):
        replace(component, statechart_types=("WorkOrderChart", "WorkOrderChart"))
    with pytest.raises(ValueError):
        replace(component, owner_id=" ")


def test_rejects_dangling_self_and_duplicate_causal_boundaries():
    spec = organization()
    for changes in (
        {"producer_id": "missing"},
        {"consumer_id": "missing"},
        {"consumer_id": "sales"},
        {"contract_version": 0},
    ):
        # Dataclass __post_init__ can itself reject the invalid value.
        # Construct it *inside* the exception context.
        with pytest.raises(ValueError):
            replace(spec, boundaries=(replace(spec.boundaries[0], **changes),))
    with pytest.raises(ValueError):
        replace(spec, boundaries=spec.boundaries * 2)


def test_rejects_duplicate_job_identity_and_invalid_component_link():
    spec = organization()
    cases = (
        lambda: spec.jobs + (spec.jobs[0],),
        lambda: (replace(spec.jobs[0], component_id="missing"),),
        lambda: (replace(spec.jobs[0], trigger_namespace=""),),
        lambda: (spec.jobs[0], replace(spec.jobs[1], trigger_namespace="order")),
    )
    for make_jobs in cases:
        with pytest.raises(ValueError):
            replace(spec, jobs=make_jobs())


def test_physical_pool_ref_is_not_a_second_capacity_ledger():
    spec = organization()
    pool = spec.physical_pools[0]
    assert not hasattr(pool, "available_units")
    assert not hasattr(pool, "reservations")
    with pytest.raises(ValueError):
        replace(spec, physical_pools=(replace(pool, consumer_ids=("missing",)),))
    with pytest.raises(ValueError):
        replace(spec, physical_pools=(pool, pool))


def test_flow_correlation_is_not_automatically_organization_identity():
    spec = organization()
    assert spec.organization_id not in {j.job_id for j in spec.jobs}
    assert "correlation_id" not in ProcessComponent.__dataclass_fields__
    assert "correlation_id" not in OrganizationDefinition.__dataclass_fields__


def _config(*, component="sales", kind="calendar", version=1, epoch=0,
            source="a" * 64):
    return OperationalConfigReference(
        component_id=component, kind=kind, reference_id=f"certified-ref:{kind}",
        reference_version=version, source_sha256=source, config_epoch=epoch,
    )


def test_operational_reference_is_versioned_immutable_and_fingerprinted():
    spec = organization()
    references = (_config(kind="calendar"), _config(kind="policy"))
    admitted_structure = replace(spec, operational_configs=references)
    assert admitted_structure.fingerprint() != spec.fingerprint()
    assert admitted_structure.fingerprint() == replace(
        admitted_structure, operational_configs=tuple(reversed(references)),
    ).fingerprint()
    assert admitted_structure.fingerprint() != replace(
        admitted_structure,
        operational_configs=(_config(kind="calendar", version=2), references[1]),
    ).fingerprint()
    assert admitted_structure.fingerprint() != replace(
        admitted_structure,
        operational_configs=(_config(kind="calendar", source="b" * 64), references[1]),
    ).fingerprint()
    assert not hasattr(references[0], "available_capacity")
    assert not hasattr(references[0], "is_active")


@pytest.mark.parametrize("kw", (
    {"component": ""},
    {"kind": "made_up_policy"},
    {"kind": []},
    {"kind": {}},
    {"version": True},
    {"version": 0},
    {"epoch": -1},
    {"source": "A" * 64},
    {"source": "not-a-hash"},
    {"source": ""},
))
def test_bad_operational_ref_is_refused_before_runtime(kw):
    with pytest.raises(ValueError):
        _config(**kw)


def test_operational_reference_cannot_alias_another_owner_or_configuration_epoch():
    spec = organization()
    with pytest.raises(ValueError, match="dangling"):
        replace(spec, operational_configs=(_config(component="wrong-owner"),))
    with pytest.raises(ValueError, match="epoch"):
        replace(spec, operational_configs=(_config(epoch=1),))
    with pytest.raises(ValueError, match="duplicate"):
        replace(spec, operational_configs=(_config(version=1), _config(version=2)))
    with pytest.raises(TypeError, match="OperationalConfigReference"):
        replace(spec, operational_configs=(object(),))
    # Distinct component and kind are valid structural refs; their claims
    # still DO NOT self-attest operational runtime capabilities.
    valid = replace(spec, operational_configs=(
        _config(component="sales", kind="policy"),
        _config(component="maintenance", kind="policy"),
        _config(component="sales", kind="calendar"),
        _config(component="sales", kind="cost_model"),
    ))
    assert len(valid.operational_configs) == 4


def test_empty_operational_references_keep_original_v1_fingerprint_bytes():
    # Constant computed from the original #499 organization payload before
    # OperationalConfigReference was introduced. Comparing the new function
    # against itself is a vacuous compatibility test.
    historic_v1_fingerprint = (
        "ef6effecd224f8974e370ec1233e47d1318ec5b541e0d5b58bf5749f1e7b1355"
    )
    spec = organization()
    assert spec.fingerprint() == historic_v1_fingerprint
    assert replace(spec, operational_configs=()).fingerprint() == historic_v1_fingerprint
    assert replace(spec, operational_configs=(_config(),)).fingerprint() != historic_v1_fingerprint
