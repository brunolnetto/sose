import json

import pytest

from sose.cli import run_cli
from sose.examples.catalog import builtin_catalog
from sose.jobs.config import load_sose_config
from sose.jobs.scaffold import render_sose_toml, write_sose_toml


@pytest.mark.parametrize("domain_name", builtin_catalog().names())
def test_scaffold_roundtrips_every_builtin_domain(domain_name, tmp_path):
    definition = builtin_catalog().get(domain_name)
    rendered = render_sose_toml(
        definition,
        job_id=f"{domain_name}-test",
        persistence_path=f"state/{domain_name}.sqlite3",
    )

    path = tmp_path / f"{domain_name}.toml"
    path.write_text(rendered, encoding="utf-8")
    parsed, _ = load_sose_config(path)
    restored = definition.parse_config(parsed.domain.parameters)

    assert restored == definition.default_config()
    assert parsed.domain.name == domain_name
    assert parsed.job.id == f"{domain_name}-test"
    assert parsed.persistence.adapter == "sqlite_incremental"
    assert parsed.job.ticks_per_trigger == 1
    assert parsed.job.max_ticks_per_trigger == 100


def test_write_scaffold_refuses_overwrite_without_force(tmp_path):
    definition = builtin_catalog().get("mro")
    path = tmp_path / "sose.toml"

    write_sose_toml(path, definition)

    with pytest.raises(FileExistsError, match="use force"):
        write_sose_toml(path, definition)

    write_sose_toml(path, definition, job_id="replacement", force=True)
    parsed, _ = load_sose_config(path)
    assert parsed.job.id == "replacement"


def test_cli_init_generates_editable_mro_configuration(tmp_path, capsys):
    output = tmp_path / "sose.toml"

    assert run_cli(
        [
            "init",
            "--domain",
            "mro",
            "--output",
            str(output),
            "--job-id",
            "plant-maintenance",
            "--persistence",
            "sqlite_incremental",
            "--persistence-path",
            "state/mro.sqlite3",
        ]
    ) == 0

    assert capsys.readouterr().out.strip() == str(output)
    parsed, _ = load_sose_config(output)
    assert parsed.domain.name == "mro"
    assert parsed.job.id == "plant-maintenance"
    assert parsed.domain.parameters["quantity"] == 1.0
    assert parsed.domain.parameters["auto_seed_spare_parts"] is True


def test_cli_init_output_validates_without_manual_repair(tmp_path, capsys):
    output = tmp_path / "sose.toml"
    assert run_cli(
        ["init", "--domain", "tutorial_job", "--output", str(output)]
    ) == 0
    capsys.readouterr()

    assert run_cli(["validate", "--config", str(output)]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["domain"] == "tutorial_job"


def test_cli_trigger_uses_persisted_batch_policy(tmp_path, capsys):
    output = tmp_path / "sose.toml"
    assert run_cli(
        ["init", "--domain", "tutorial_job", "--output", str(output)]
    ) == 0
    capsys.readouterr()

    text = output.read_text(encoding="utf-8")
    text = text.replace(
        "ticks_per_trigger = 1",
        "ticks_per_trigger = 2",
    )
    output.write_text(text, encoding="utf-8")

    assert run_cli(
        [
            "trigger",
            "--config",
            str(output),
            "--trigger-id",
            "scheduler-001",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["completed_ticks"] == 2
    assert payload["end_tick"] == 2

    assert run_cli(
        [
            "trigger",
            "--config",
            str(output),
            "--trigger-id",
            "scheduler-001",
        ]
    ) == 0
    replayed = json.loads(capsys.readouterr().out)
    assert replayed == payload


def test_cli_init_generates_postgres_environment_config(tmp_path, capsys):
    output = tmp_path / "sose.toml"

    assert run_cli(
        [
            "init",
            "--domain",
            "mro",
            "--output",
            str(output),
            "--job-id",
            "plant-maintenance",
            "--persistence",
            "postgres",
        ]
    ) == 0
    capsys.readouterr()

    parsed, _ = load_sose_config(output)
    assert parsed.persistence.adapter == "postgres"
    assert parsed.persistence.options["dsn_env"] == "SOSE_DATABASE_URL"
    assert parsed.persistence.options["namespace"] == "plant_maintenance"
    assert "path" not in parsed.persistence.options



def test_postgres_namespace_is_ascii_safe_for_unicode_job_id(tmp_path):
    definition = builtin_catalog().get("mro")
    rendered = render_sose_toml(
        definition,
        job_id="münchen-maintenance",
        persistence_adapter="postgres",
    )
    path = tmp_path / "unicode-postgres.toml"
    path.write_text(rendered, encoding="utf-8")

    parsed, _ = load_sose_config(path)
    namespace = parsed.persistence.options["namespace"]

    assert namespace == "m_nchen_maintenance"
    assert namespace.isascii()


def test_postgres_namespace_keeps_long_job_ids_distinct(tmp_path):
    definition = builtin_catalog().get("mro")
    prefix = "maintenance-production-line-" + ("x" * 30)
    first_id = prefix + "-alpha"
    second_id = prefix + "-beta"

    namespaces = []
    for ordinal, job_id in enumerate((first_id, second_id), start=1):
        rendered = render_sose_toml(
            definition,
            job_id=job_id,
            persistence_adapter="postgres",
        )
        path = tmp_path / f"long-postgres-{ordinal}.toml"
        path.write_text(rendered, encoding="utf-8")
        parsed, _ = load_sose_config(path)
        namespaces.append(parsed.persistence.options["namespace"])

    assert namespaces[0] != namespaces[1]
    assert all(len(namespace) <= 40 for namespace in namespaces)
    assert all(namespace.isascii() for namespace in namespaces)


def test_scaffold_annotates_domain_parameters_for_editing():
    rendered = render_sose_toml(builtin_catalog().get("mro"))

    assert "# Generated by SOSE." in rendered
    assert "# Edit [domain.parameters], then run: sose validate && sose apply" in rendered
    assert "# Quantity" in rendered
    assert "# type=number; > 0; max=100.0" in rendered
    assert "# Technician Capacity" in rendered
    assert "# type=integer; min=1" in rendered


def test_scaffold_comments_do_not_change_config_roundtrip(tmp_path):
    definition = builtin_catalog().get("mro")
    path = tmp_path / "annotated.toml"
    path.write_text(render_sose_toml(definition), encoding="utf-8")

    parsed, _ = load_sose_config(path)
    assert definition.parse_config(parsed.domain.parameters) == (
        definition.default_config()
    )


def test_scaffold_marks_runtime_and_bootstrap_parameter_mutability():
    rendered = render_sose_toml(builtin_catalog().get("mro"))

    tick_block = rendered.split("tick_step =", 1)[0]
    assert "# mutability=runtime" in tick_block
    quantity_prefix = rendered.split("quantity =", 1)[0]
    assert "# mutability=bootstrap" in quantity_prefix
