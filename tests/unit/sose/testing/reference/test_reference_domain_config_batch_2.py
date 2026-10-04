from datetime import timedelta

from sose.examples.catalog import builtin_catalog
from sose.persistence.memory import MemoryPersistence


BATCH = {
    "aviation",
    "cards_payments",
    "energy_utilities",
    "field_service",
    "hospitality",
    "logistics",
    "p2p",
    "record_to_report",
}


def test_second_reference_config_batch_is_registered():
    names = set(builtin_catalog().names())
    assert BATCH <= names


def test_second_reference_configs_roundtrip_and_drive_runtime_tick_step():
    catalog = builtin_catalog()
    for name in sorted(BATCH):
        definition = catalog.get(name)
        config = definition.default_config()
        restored = definition.config_model.model_validate_json(
            config.model_dump_json()
        )
        assert restored == config

        persistence = MemoryPersistence()
        definition.seed(persistence, config)
        context, _ = definition.build_runtime(
            persistence,
            config,
            config.start_at,
            0,
        )
        assert context.clock.step == config.tick_step


def test_second_batch_existing_seed_knobs_are_typed():
    catalog = builtin_catalog()

    p2p = catalog.get("p2p").parse_config({"quantity": 44.0})
    assert p2p.quantity == 44.0

    r2r = catalog.get("record_to_report").parse_config(
        {"amount": 3200.0, "currency": "BRL"}
    )
    assert r2r.amount == 3200.0
    assert r2r.currency == "BRL"


def test_second_batch_common_runtime_config_can_be_overridden():
    definition = builtin_catalog().get("field_service")
    config = definition.parse_config(
        {
            "tick_step": timedelta(minutes=10),
            "random_seed": 9911,
        }
    )
    persistence = MemoryPersistence()
    definition.seed(persistence, config)
    context, _ = definition.build_runtime(
        persistence,
        config,
        config.start_at,
        0,
    )
    assert context.clock.step == timedelta(minutes=10)
