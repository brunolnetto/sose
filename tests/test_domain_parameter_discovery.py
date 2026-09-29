import json
from enum import Enum

from pydantic import BaseModel

from sose.domain.config import DomainConfig, DomainDefinition
from sose.examples.catalog import builtin_catalog


BASE_FIELDS = set(DomainConfig.model_fields)


def test_every_builtin_domain_description_is_json_serializable():
    for definition in builtin_catalog().definitions():
        description = definition.describe_config()
        assert json.dumps(description)
        assert description["name"] == definition.name
        assert description["defaults"] == (
            definition.default_config().model_dump(mode="json")
        )


def test_every_builtin_domain_description_covers_all_config_fields():
    for definition in builtin_catalog().definitions():
        description = definition.describe_config()
        described = {item["name"] for item in description["parameters"]}
        assert described == set(definition.config_model.model_fields)


def test_every_builtin_domain_description_marks_domain_specific_fields():
    for definition in builtin_catalog().definitions():
        description = definition.describe_config()
        domain_specific = {
            item["name"]
            for item in description["parameters"]
            if item["name"] not in BASE_FIELDS
        }
        assert domain_specific, definition.name


def test_description_preserves_referenced_schema_definitions():
    class Mode(str, Enum):
        normal = "normal"
        urgent = "urgent"

    class Nested(BaseModel):
        threshold: int = 3

    class CustomConfig(DomainConfig):
        start_at: str = "2026-01-01T00:00:00Z"
        mode: Mode = Mode.normal
        nested: Nested = Nested()

    definition = DomainDefinition(
        name="custom",
        description="custom discovery test",
        config_model=CustomConfig,
        build_runtime=lambda persistence, config, now, tick: (None, None),
        seed=lambda persistence, config: None,
    )

    description = definition.describe_config()
    assert "Mode" in description["$defs"]
    assert "Nested" in description["$defs"]

    schemas = {
        item["name"]: item["schema"]
        for item in description["parameters"]
    }
    assert schemas["mode"]["$ref"] == "#/$defs/Mode"
    assert schemas["nested"]["$ref"] == "#/$defs/Nested"


def test_description_uses_aliases_consistently_for_defaults_and_schema():
    from pydantic import Field

    class AliasConfig(DomainConfig):
        start_at: str = "2026-01-01T00:00:00Z"
        internal_name: int = Field(default=7, alias="externalName")

    definition = DomainDefinition(
        name="alias",
        description="alias discovery test",
        config_model=AliasConfig,
        build_runtime=lambda persistence, config, now, tick: (None, None),
        seed=lambda persistence, config: None,
    )

    description = definition.describe_config()
    assert description["defaults"]["externalName"] == 7
    assert "internal_name" not in description["defaults"]

    by_name = {
        item["name"]: item
        for item in description["parameters"]
    }
    assert by_name["externalName"]["default"] == 7
