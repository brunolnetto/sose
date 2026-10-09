"""Prospective protocols must not silently drift from audited Gate-A adapters."""
from __future__ import annotations

import pytest

from sose.organizational.agency import AgencyLevel
from sose.organizational.experiment import ExperimentProtocol
from sose.organizational.o2c_protocol import build_o2c_official_plan_v1
from sose.organizational.mro_protocol import build_mro_official_plan_v1
from sose.organizational.o2c_reference import O2CReferenceDomain
from sose.organizational.mro_reference import MROReferenceDomain


@pytest.mark.parametrize(
    "plan_builder,reference,axis,arms",
    [
        (build_o2c_official_plan_v1, O2CReferenceDomain, "due_delay_hours",
         ("partial_fulfillment", "overdue_collection")),
        (build_mro_official_plan_v1, MROReferenceDomain, "quantity",
         ("spare_part_shortage", "emergency_preemption")),
    ],
)
def test_official_design_is_distinctly_declared_and_reproducible(
    plan_builder, reference, axis, arms,
):
    plan = plan_builder()
    assert plan.plan_hash == plan_builder().plan_hash
    assert plan.protocol == ExperimentProtocol.model_validate(plan.protocol.canonical_payload())
    assert plan.protocol.intervention_ids == arms
    assert set(plan.protocol.parameter_ranges) == {axis}
    assert plan.protocol.sample_size == 6
    assert (plan.design_seed, plan.root_seed) == (20261008, 20261008)
    assert plan.protocol.replication_plan.min_replications == 2
    assert plan.protocol.replication_plan.max_replications == 2
    assert plan.protocol.warmup == 0.0
    assert plan.protocol.horizon == 24.0
    assert plan.protocol.agency_levels == (AgencyLevel.A0,)
    assert plan.protocol.baseline_model_spec_hash == reference().build_model(
        {axis: next(p.default for p in reference().descriptor.parameters if p.name == axis)}
    ).model_spec_hash
    assert len(plan.plan_hash) == 64
    assert len(plan.protocol.protocol_hash) == 64


def test_distinct_domains_do_not_share_protocol_identity():
    assert build_o2c_official_plan_v1().plan_hash != build_mro_official_plan_v1().plan_hash
