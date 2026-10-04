import json

from sose.cli import run_cli
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


def test_mro_declarative_recurring_job_applies_runtime_config_on_next_trigger(
    tmp_path,
    capsys,
):
    config = tmp_path / "sose.toml"
    state_path = tmp_path / "state" / "mro.sqlite3"

    assert run_cli(
        [
            "init",
            "--domain",
            "mro",
            "--job-id",
            "mro-recurring-e2e",
            "--persistence",
            "sqlite_incremental",
            "--persistence-path",
            "state/mro.sqlite3",
            "--output",
            str(config),
        ]
    ) == 0
    capsys.readouterr()

    assert run_cli(
        [
            "config",
            "set",
            "quantity",
            "2.0",
            "--config",
            str(config),
        ]
    ) == 0
    capsys.readouterr()
    assert run_cli(
        [
            "config",
            "set",
            "auto_seed_spare_parts",
            "false",
            "--config",
            str(config),
        ]
    ) == 0
    capsys.readouterr()

    assert run_cli(["validate", "--config", str(config)]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["domain"] == "mro"
    assert validated["domain_parameters"]["quantity"] == 2.0
    assert validated["domain_parameters"]["auto_seed_spare_parts"] is False

    assert run_cli(
        [
            "trigger",
            "--config",
            str(config),
            "--scheduled-for",
            "2026-03-01T09:00:00Z",
        ]
    ) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["end_tick"] == 1
    assert first["config_revision"] == 1

    persistence = SQLiteIncrementalPersistence(state_path)
    job_state = persistence.job_state("mro-recurring-e2e")
    assert job_state is not None
    work_order_id = job_state.bootstrap_state.work_order_id
    work_order = persistence.entity("work_order", work_order_id)
    assert work_order is not None
    assert work_order.state == "waiting_material"
    assert persistence.store_get_results() == ()
    assert persistence.container_operation_results() == ()
    persistence.close()

    assert run_cli(
        [
            "config",
            "set",
            "auto_seed_spare_parts",
            "true",
            "--config",
            str(config),
        ]
    ) == 0
    changed = json.loads(capsys.readouterr().out)
    assert changed["mutability"] == "runtime"
    assert changed["value"] is True
    assert changed["applied"] is False

    assert run_cli(["apply", "--config", str(config)]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["changed"] is True
    assert applied["config_revision"] == 2
    assert applied["config"]["auto_seed_spare_parts"] is True

    assert run_cli(
        [
            "trigger",
            "--config",
            str(config),
            "--scheduled-for",
            "2026-03-01T10:00:00Z",
        ]
    ) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["start_tick"] == 1
    assert second["end_tick"] == 2
    assert second["config_revision"] == 2

    reopened = SQLiteIncrementalPersistence(state_path)
    final_state = reopened.job_state("mro-recurring-e2e")
    final_work_order = reopened.entity("work_order", work_order_id)

    assert final_state is not None
    assert final_state.config_revision == 2
    assert final_state.next_tick == 2
    assert final_work_order is not None
    assert final_work_order.state == "in_progress"
    assert reopened.store_get_results()
    assert reopened.container_operation_results()
    assert reopened.simulation_position().logical_tick == 2
    reopened.close()

    assert run_cli(["doctor", "--config", str(config)]) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["healthy"] is True
