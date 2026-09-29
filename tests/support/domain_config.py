from __future__ import annotations

from datetime import timedelta
from enum import Enum

import pytest


BASE_RUNTIME_FIELDS = {"tick_step", "random_seed"}


def domain_specific_runtime_fields(definition) -> tuple[str, ...]:
    return tuple(
        sorted(set(definition.runtime_mutable_fields) - BASE_RUNTIME_FIELDS)
    )


def alternate_runtime_value(definition, config, field_name: str):
    """Return a valid alternate value for a runtime-mutable domain field."""

    current = getattr(config, field_name)
    metadata = {
        item["field_name"]: item
        for item in definition.describe_config()["parameters"]
    }[field_name]
    schema = metadata["schema"]

    if isinstance(current, bool):
        return not current

    if isinstance(current, Enum):
        values = list(type(current))
        if len(values) < 2:
            pytest.skip(
                f"{definition.name}.{field_name} has no alternate enum value"
            )
        index = values.index(current)
        return values[(index + 1) % len(values)]

    if isinstance(current, timedelta):
        return current + timedelta(minutes=1)

    if isinstance(current, int) and not isinstance(current, bool):
        maximum = schema.get("maximum")
        exclusive_maximum = schema.get("exclusiveMaximum")
        minimum = schema.get("minimum")
        exclusive_minimum = schema.get("exclusiveMinimum")

        candidate = current + 1
        if maximum is not None and candidate > maximum:
            candidate = current - 1
        if exclusive_maximum is not None and candidate >= exclusive_maximum:
            candidate = current - 1
        if minimum is not None and candidate < minimum:
            candidate = current + 1
        if exclusive_minimum is not None and candidate <= exclusive_minimum:
            candidate = current + 1
        return candidate

    if isinstance(current, float):
        maximum = schema.get("maximum")
        exclusive_maximum = schema.get("exclusiveMaximum")
        minimum = schema.get("minimum")
        exclusive_minimum = schema.get("exclusiveMinimum")

        candidate = current + 1.0
        if maximum is not None and candidate > maximum:
            candidate = current - 1.0
        if exclusive_maximum is not None and candidate >= exclusive_maximum:
            candidate = current - 1.0
        if minimum is not None and candidate < minimum:
            candidate = current + 1.0
        if exclusive_minimum is not None and candidate <= exclusive_minimum:
            candidate = current + 1.0
        return candidate

    if isinstance(current, str):
        enum_values = schema.get("enum")
        if not enum_values:
            for branch in schema.get("anyOf", ()):
                values = branch.get("enum")
                if values:
                    enum_values = values
                    break
        if enum_values:
            alternatives = [value for value in enum_values if value != current]
            if not alternatives:
                pytest.skip(
                    f"{definition.name}.{field_name} has no alternate literal"
                )
            return alternatives[0]
        return f"{current}-next"

    raise AssertionError(
        f"no generic runtime mutation strategy for "
        f"{definition.name}.{field_name}: {type(current).__name__}"
    )
