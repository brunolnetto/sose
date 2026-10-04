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


def test_apply_config_replaces_runtime_mutable_desired_config_once():
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
            "complete_after": "PT2H",
        }
    )
    repeated = job.apply_config(
        {
            "random_seed": 99,
            "complete_after": "PT2H",
        }
    )

    assert changed.config_revision == 2
    assert repeated.config_revision == 2
    payload = json.loads(changed.config_json)
    assert payload["random_seed"] == 99
    assert payload["complete_after"] == "PT2H"


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


def test_job_section_rejects_ticks_per_trigger_above_max(tmp_path):
    from pydantic import ValidationError
    from sose.jobs.config import load_sose_config

    path = tmp_path / "invalid-trigger.toml"
    path.write_text(
        """
[domain]
name = "tutorial_job"

[persistence]
adapter = "memory"

[job]
id = "invalid"
ticks_per_trigger = 5
max_ticks_per_trigger = 2
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="ticks_per_trigger"):
        load_sose_config(path)


def test_apply_config_rejects_bootstrap_only_change_after_initialization():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("tutorial_job")
    job = SimulationJob(
        job_id="apply-bootstrap-blocked",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )
    job.initialize()

    with pytest.raises(ValueError, match="bootstrap-only.*complete_after"):
        job.apply_config({"complete_after": "PT4H"})

    state = job.state()
    assert state is not None
    assert state.config_revision == 1
    assert json.loads(state.config_json)["complete_after"] == "PT2H"


def test_mro_runtime_policy_can_change_but_bootstrap_quantity_cannot():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("mro")
    job = SimulationJob(
        job_id="mro-config-mutability",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )
    initial = job.initialize()

    changed = job.update_config({"auto_seed_spare_parts": False})
    assert changed.config_revision == initial.config_revision + 1
    assert json.loads(changed.config_json)["auto_seed_spare_parts"] is False

    with pytest.raises(ValueError, match="bootstrap-only.*quantity"):
        job.update_config({"quantity": 2.0})

    assert job.state().config_revision == changed.config_revision
