from sose.domain.config import DomainConfig
from sose.examples.catalog import builtin_catalog


BASE_FIELDS = set(DomainConfig.model_fields)


def test_every_builtin_domain_exposes_custom_parameters():
    catalog = builtin_catalog()

    assert len(catalog.names(kind="domain")) == 22
    assert len(catalog.names(kind="canonical")) == 5

    for definition in catalog.definitions():
        domain_fields = set(definition.config_model.model_fields) - BASE_FIELDS
        assert domain_fields, (
            f"{definition.name} must expose at least one domain-specific "
            "configuration field"
        )


def test_every_builtin_domain_default_config_roundtrips():
    for definition in builtin_catalog().definitions():
        config = definition.default_config()
        payload = config.model_dump_json()
        restored = definition.config_model.model_validate_json(payload)

        assert restored == config


def test_every_builtin_domain_schema_forbids_unknown_parameters():
    for definition in builtin_catalog().definitions():
        schema = definition.config_model.model_json_schema()
        assert schema.get("additionalProperties") is False, definition.name
