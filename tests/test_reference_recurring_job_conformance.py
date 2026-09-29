from __future__ import annotations

from datetime import timedelta
from enum import Enum

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.catalog import builtin_catalog
from sose.jobs.runner import SimulationJob
from sose.persistence.memory import MemoryPersistence


BASE_RUNTIME_FIELDS = {"tick_step", "random_seed"}


def _backend(origin):
    return SimPyBackend(origin=origin)


def _alternate_runtime_value(definition, config, field_name: str):
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


@pytest.mark.parametrize(
    "domain_name",
    [
        name
        for name in builtin_catalog().names()
        if name != "tutorial_job"
    ],
)
def test_every_reference_accepts_domain_specific_config_revision_between_ticks(
    domain_name,
):
    definition = builtin_catalog().get(domain_name)
    domain_fields = sorted(
        set(definition.runtime_mutable_fields) - BASE_RUNTIME_FIELDS
    )
    assert domain_fields, domain_name
    assert definition.reconcile_tick is not None, domain_name

    config = definition.default_config()
    field_name = domain_fields[0]
    alternate = _alternate_runtime_value(
        definition,
        config,
        field_name,
    )
    updated_config = definition.parse_config(
        {
            **config.model_dump(mode="python"),
            field_name: alternate,
        }
    )
    assert getattr(updated_config, field_name) != getattr(config, field_name)

    persistence = MemoryPersistence()
    job = SimulationJob(
        job_id=f"{domain_name}-recurring-conformance",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )

    initialized = job.initialize(config)
    first = job.run_tick(trigger_id=f"{domain_name}:tick:1")

    revised = job.update_config({field_name: alternate})
    second = job.run_tick(trigger_id=f"{domain_name}:tick:2")

    final = job.state()
    assert initialized.config_revision == 1
    assert first.config_revision == 1
    assert revised.config_revision == 2
    assert second.config_revision == 2
    assert final is not None
    assert final.status == "ready"
    assert final.phase == "idle"
    assert final.next_tick == 2
    assert final.run_count == 2
    assert getattr(
        definition.config_model.model_validate_json(final.config_json),
        field_name,
    ) == getattr(updated_config, field_name)
