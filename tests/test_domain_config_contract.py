from datetime import timedelta

import pytest

from sose.examples.catalog import builtin_catalog
from sose.examples.mro.config import MROConfig
from sose.persistence.memory import MemoryPersistence


def test_builtin_catalog_exposes_parameterized_domains():
    catalog = builtin_catalog()
    assert catalog.names() == ("mro", "tutorial_job")


def test_mro_config_validates_and_accepts_overrides():
    config = MROConfig.model_validate(
        {
            "quantity": 4.0,
            "technician_capacity": 3,
            "maintenance_bay_capacity": 2,
            "spare_part_store_capacity": 25,
            "release_delay": timedelta(hours=3),
            "random_seed": 99,
            "tick_step": timedelta(minutes=30),
        }
    )

    assert config.quantity == 4.0
    assert config.technician_capacity == 3
    assert config.release_delay == timedelta(hours=3)
    assert config.random_seed == 99
    assert config.tick_step == timedelta(minutes=30)


def test_domain_config_rejects_unknown_keys():
    with pytest.raises(Exception):
        MROConfig.model_validate({"not_a_real_parameter": 1})


def test_mro_definition_applies_runtime_and_seed_configuration():
    definition = builtin_catalog().get("mro")
    config = definition.parse_config(
        {
            "quantity": 2.0,
            "technician_capacity": 2,
            "maintenance_bay_capacity": 2,
            "spare_part_store_capacity": 15,
            "release_delay": timedelta(hours=4),
            "random_seed": 77,
            "tick_step": timedelta(minutes=20),
        }
    )
    persistence = MemoryPersistence()

    ids = definition.seed(persistence, config)
    context, _ = definition.build_runtime(
        persistence,
        config,
        config.start_at,
        0,
    )

    assert context.clock.step == timedelta(minutes=20)
    assert persistence.entity("work_order", ids.work_order_id).attributes["quantity"] == 2.0
    assert persistence.resource_definitions()[0].capacity == 2
    assert persistence.preemptive_resource_definitions()[0].capacity == 2
    assert persistence.store_definitions()[0].capacity == 15
    assert persistence.scheduled_work()[0].due_at == (
        config.start_at + timedelta(hours=4)
    )


def test_definition_config_roundtrips_through_json():
    definition = builtin_catalog().get("mro")
    config = definition.default_config()
    restored = definition.config_model.model_validate_json(
        config.model_dump_json()
    )
    assert restored == config
