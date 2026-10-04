from datetime import timedelta

from sose.examples.catalog import builtin_catalog
from sose.persistence.memory import MemoryPersistence


BATCH = {
    "airports",
    "construction",
    "credit_loans",
    "hospitals",
    "insurance",
    "itsm",
    "manufacturing",
    "order_to_cash",
}


def test_first_reference_config_batch_is_registered():
    names = set(builtin_catalog().names())
    assert BATCH <= names


def test_first_reference_configs_roundtrip_and_drive_runtime_tick_step():
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


def test_existing_seed_knobs_are_exposed_by_typed_configs():
    catalog = builtin_catalog()

    assert catalog.get("airports").parse_config(
        {"flight_number": "CFG123", "departure_priority": 7}
    ).flight_number == "CFG123"

    assert catalog.get("construction").parse_config(
        {"quantity": 22.0, "predecessor_completed": False}
    ).quantity == 22.0

    credit = catalog.get("credit_loans").parse_config(
        {"principal": 2500.0, "installment_count": 5}
    )
    assert credit.principal == 2500.0
    assert credit.installment_count == 5

    assert catalog.get("hospitals").parse_config({"acuity": 90}).acuity == 90

    insurance = catalog.get("insurance").parse_config(
        {
            "severity": 80,
            "amount": 9000.0,
            "claim_type": "liability",
            "currency": "BRL",
        }
    )
    assert insurance.currency == "BRL"
    assert insurance.amount == 9000.0

    assert catalog.get("itsm").parse_config({"severity": 10}).severity == 10
    assert catalog.get("manufacturing").parse_config({"quantity": 18.0}).quantity == 18.0
    assert catalog.get("order_to_cash").parse_config({"amount": 777.0}).amount == 777.0


def test_runtime_common_overrides_are_not_domain_specific():
    definition = builtin_catalog().get("insurance")
    config = definition.parse_config(
        {
            "tick_step": timedelta(minutes=15),
            "random_seed": 12345,
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
    assert context.clock.step == timedelta(minutes=15)
