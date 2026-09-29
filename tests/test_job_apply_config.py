import json
from dataclasses import replace

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.catalog import builtin_catalog
from sose.jobs.runner import SimulationJob
from sose.persistence.memory import MemoryPersistence


def _backend(origin):
    return SimPyBackend(origin=origin)


def test_apply_config_is_idempotent_when_desired_config_is_unchanged():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("tutorial_job")
    job = SimulationJob(
        job_id="apply-idempotent",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )
    initial = job.initialize({"random_seed": 11})

    applied = job.apply_config(
        definition.config_model.model_validate_json(initial.config_json)
    )

    assert applied == initial
    assert applied.config_revision == 1


def test_apply_config_replaces_complete_desired_config_once():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("tutorial_job")
    job = SimulationJob(
        job_id="apply-change",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )
    job.initialize({"random_seed": 11})

    changed = job.apply_config(
        {
            "random_seed": 99,
            "complete_after": "PT4H",
        }
    )
    repeated = job.apply_config(
        {
            "random_seed": 99,
            "complete_after": "PT4H",
        }
    )

    assert changed.config_revision == 2
    assert repeated.config_revision == 2
    payload = json.loads(changed.config_json)
    assert payload["random_seed"] == 99
    assert payload["complete_after"] == "PT4H"


def test_apply_config_rejects_real_change_while_trigger_is_owned():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("tutorial_job")
    job = SimulationJob(
        job_id="apply-owned",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )
    initial = job.initialize()

    with persistence.transaction() as uow:
        current = uow.get_job_state(job.job_id)
        assert current is not None
        uow.save_job_state(
            replace(
                current,
                status="running",
                active_trigger_id="scheduler-1",
                phase="advance",
            )
        )

    with pytest.raises(RuntimeError, match="cannot change config"):
        job.apply_config({"random_seed": initial.config_revision + 100})


def test_job_section_validates_recurring_trigger_policy(tmp_path):
    from sose.jobs.config import load_sose_config

    path = tmp_path / "sose.toml"
    path.write_text(
        """
[domain]
name = "tutorial_job"

[domain.parameters]

[persistence]
adapter = "memory"

[runtime]
backend = "simpy"

[job]
id = "recurring"
ticks_per_trigger = 3
max_ticks_per_trigger = 7
""".strip(),
        encoding="utf-8",
    )

    config, _ = load_sose_config(path)

    assert config.job.ticks_per_trigger == 3
    assert config.job.max_ticks_per_trigger == 7
