import json

from sose.domain.config import DomainConfig
from sose.examples.catalog import builtin_catalog


BASE_FIELDS = set(DomainConfig.model_fields)


def test_every_builtin_domain_description_is_json_serializable():
    for definition in builtin_catalog().definitions():
        description = definition.describe_config()
        encoded = json.dumps(description)

        assert encoded
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
