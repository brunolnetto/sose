from __future__ import annotations

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.catalog import builtin_catalog
from sose.jobs.runner import SimulationJob
from sose.persistence.memory import MemoryPersistence
from tests.support.domain_config import (
    alternate_runtime_value,
    domain_specific_runtime_fields,
)


def _backend(origin):
    return SimPyBackend(origin=origin)


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
    domain_fields = domain_specific_runtime_fields(definition)
    assert domain_fields, domain_name
    assert definition.reconcile_tick is not None, domain_name

    config = definition.default_config()
    field_name = domain_fields[0]
    alternate = alternate_runtime_value(
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
