from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from sose.dsl.loader import load_domain
from sose.dsl.models import DomainSpec, EntitySpec, RelationshipSpec, ScenarioSpec


def test_domain_models_validate_complete_spec():
    spec = DomainSpec(
        name="logistics",
        entities=[
            EntitySpec(name="shipment", statechart="ShipmentChart"),
            EntitySpec(name="hub_event", statechart="HubEventChart", kind="event"),
        ],
        relationships=[
            RelationshipSpec(
                name="shipment_events",
                from_entity="shipment",
                to_entity="hub_event",
                cardinality="one-to-many",
            )
        ],
        scenarios=[
            ScenarioSpec(
                name="congestion",
                entity="shipment",
                trigger="per_tick",
                policy="congestion_policy",
                activation_probability=0.25,
            )
        ],
    )

    assert spec.entities[0].kind == "stateful"
    assert spec.relationships[0].cardinality == "one-to-many"
    assert spec.scenarios[0].activation_probability == 0.25


def test_load_domain_reads_yaml_and_applies_defaults(tmp_path: Path):
    path = tmp_path / "domain.yml"
    path.write_text(
        """
name: retail
entities:
  - name: order
    statechart: OrderChart
relationships: []
scenarios:
  - name: peak
    entity: order
    trigger: scheduled
    policy: peak_policy
""".strip(),
        encoding="utf-8",
    )

    spec = load_domain(path)

    assert spec.name == "retail"
    assert spec.entities[0].kind == "stateful"
    assert spec.scenarios[0].activation_probability is None


@pytest.mark.parametrize("probability", [-0.1, 1.1])
def test_scenario_probability_is_bounded(probability):
    with pytest.raises(ValidationError):
        ScenarioSpec(
            name="invalid",
            entity="order",
            trigger="on_event",
            policy="policy",
            activation_probability=probability,
        )


def test_invalid_relationship_cardinality_is_rejected():
    with pytest.raises(ValidationError):
        RelationshipSpec(
            name="bad",
            from_entity="a",
            to_entity="b",
            cardinality="many-to-everything",
        )


def test_load_domain_propagates_validation_errors(tmp_path: Path):
    path = tmp_path / "invalid.yml"
    path.write_text(
        """
name: invalid
entities:
  - name: order
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_domain(path)
