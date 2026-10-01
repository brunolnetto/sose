from sose.examples.catalog import builtin_catalog
from sose.persistence.memory import MemoryPersistence


EXPECTED = {
    "airports",
    "aviation",
    "cards_payments",
    "construction",
    "credit_loans",
    "dining_philosophers",
    "job_shop",
    "producer_consumer",
    "readers_writers",
    "sleeping_barber",
    "energy_utilities",
    "field_service",
    "hospitality",
    "hospitals",
    "insurance",
    "itsm",
    "logistics",
    "manufacturing",
    "mro",
    "order_to_cash",
    "p2p",
    "record_to_report",
    "subscription_saas",
    "telecom",
    "transit",
    "warehouse_fulfillment",
    "tutorial_job",
}


def test_builtin_catalog_covers_every_promoted_reference_plus_tutorial():
    catalog = builtin_catalog()
    assert set(catalog.names()) == EXPECTED
    assert len(catalog.names()) == 27


def test_every_builtin_domain_has_serializable_default_config_and_builds_runtime():
    catalog = builtin_catalog()

    for definition in catalog.definitions():
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


def test_final_reference_batch_is_parameterized():
    catalog = builtin_catalog()
    for name in (
        "subscription_saas",
        "telecom",
        "transit",
        "warehouse_fulfillment",
    ):
        definition = catalog.get(name)
        config = definition.parse_config(
            {
                "random_seed": 987654,
            }
        )
        assert config.random_seed == 987654
