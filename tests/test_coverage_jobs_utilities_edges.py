from __future__ import annotations

from pathlib import Path
import builtins

import pytest

from sose.examples.catalog import builtin_catalog
from sose.jobs.config import DomainWarehouseSection, JobSection
from sose.jobs.config_edit import _write_parameter
from sose.jobs.factory import (
    _backend_factory,
    _domain_warehouse_section,
    build_job_from_config,
)
from sose.jobs.job_edit import _write_job_section
from sose.jobs.scaffold import _schema_comment, _toml_value, render_sose_toml


def test_backend_factory_rejects_unknown_backend_name():
    with pytest.raises(KeyError, match="unknown runtime backend"):
        _backend_factory("unknown")


def test_domain_warehouse_sqlite_requires_non_empty_path(tmp_path):
    section = DomainWarehouseSection(
        adapter="sqlite",
        options={"path": ""},
    )
    with pytest.raises(ValueError, match="path must be a non-empty string"):
        _domain_warehouse_section(section, tmp_path)


def test_domain_warehouse_postgres_requires_dsn_or_env(tmp_path):
    section = DomainWarehouseSection(adapter="postgres", options={"namespace": "ns"})
    with pytest.raises(
        (ValueError, RuntimeError),
        match="requires dsn or dsn_env|requires installing the 'postgres' extra",
    ):
        _domain_warehouse_section(section, tmp_path)


def test_domain_warehouse_sqlite_rejects_unknown_options(tmp_path):
    section = DomainWarehouseSection(
        adapter="sqlite",
        options={"path": "state/domain.sqlite3", "extra": 1},
    )
    with pytest.raises(ValueError, match="unknown sqlite DomainWarehouse options"):
        _domain_warehouse_section(section, tmp_path)


def test_toml_value_rejects_unsupported_type():
    with pytest.raises(TypeError, match="unsupported TOML scaffold value"):
        _toml_value(object())


def test_render_sose_toml_rejects_non_simpy_backend():
    definition = builtin_catalog().get("producer_consumer")
    with pytest.raises(ValueError, match="built-in simulation backend"):
        render_sose_toml(definition, runtime_backend="other")


def test_render_sose_toml_postgres_namespace_is_normalized_and_capped():
    definition = builtin_catalog().get("producer_consumer")
    rendered = render_sose_toml(
        definition,
        persistence_adapter="postgres",
        job_id="9 very-long/job id with symbols that should be normalized and truncated",
    )

    namespace_line = next(
        line for line in rendered.splitlines() if line.startswith("namespace = ")
    )
    namespace = namespace_line.split("=", 1)[1].strip().strip('"')
    assert namespace.startswith("job_")
    assert len(namespace) <= 40


def test_write_job_section_rejects_missing_job_header():
    with pytest.raises(ValueError, match=r"missing \[job\] section"):
        _write_job_section("[domain]\nname = \"x\"\n", JobSection(id="j"))


def test_write_job_section_inserts_missing_policy_keys_before_next_section():
    original = "[job]\nid = \"old\"\n\n[runtime]\nbackend = \"simpy\"\n"
    updated = _write_job_section(
        original,
        JobSection(id="new", ticks_per_trigger=3, max_ticks_per_trigger=9),
    )

    assert "id = \"new\"" in updated
    assert "ticks_per_trigger = 3" in updated
    assert "max_ticks_per_trigger = 9" in updated
    assert updated.index("max_ticks_per_trigger = 9") < updated.index("[runtime]")


def test_write_parameter_inserts_missing_domain_parameter_before_next_section(tmp_path):
    config_path = Path(tmp_path) / "sose.toml"
    config_path.write_text(
        "[domain.parameters]\nexisting = 1\n\n[job]\nid = \"demo\"\n",
        encoding="utf-8",
    )

    _write_parameter(config_path, "added", 7)
    updated = config_path.read_text(encoding="utf-8")

    assert "existing = 1" in updated
    assert "added = 7" in updated
    assert updated.index("added = 7") < updated.index("[job]")


def test_write_parameter_appends_section_after_existing_blank_line(tmp_path):
    config_path = Path(tmp_path) / "sose.toml"
    config_path.write_text(
        "[domain]\nname = \"tutorial_job\"\n\n",
        encoding="utf-8",
    )

    _write_parameter(config_path, "auto_complete", False)
    assert config_path.read_text(encoding="utf-8") == (
        "[domain]\n"
        "name = \"tutorial_job\"\n"
        "\n"
        "[domain.parameters]\n"
        "auto_complete = false\n"
    )


def test_write_parameter_updates_existing_assignment_and_preserves_indent(tmp_path):
    config_path = Path(tmp_path) / "sose.toml"
    config_path.write_text(
        "[domain.parameters]\n  random_seed = 1\n",
        encoding="utf-8",
    )

    _write_parameter(config_path, "random_seed", 7)
    assert "  random_seed = 7\n" in config_path.read_text(encoding="utf-8")


def test_write_job_section_skips_non_assignment_lines_and_inserts_missing_keys():
    updated = _write_job_section(
        "[job]\n# comment\nid = \"old\"\n\n[domain]\nname = \"tutorial_job\"\n",
        JobSection(id="new", ticks_per_trigger=4, max_ticks_per_trigger=9),
    )

    assert "# comment" in updated
    assert 'id = "new"' in updated
    assert "ticks_per_trigger = 4" in updated
    assert "max_ticks_per_trigger = 9" in updated
    assert updated.index("max_ticks_per_trigger = 9") < updated.index("[domain]")


def test_schema_comment_prefers_ref_title_and_omits_empty_traits():
    comments = _schema_comment(
        {"$ref": "#/$defs/item"},
        {"item": {"title": "Capacity"}},
    )
    assert comments == ["Capacity"]


def test_toml_value_omits_none_items_inside_dict():
    assert _toml_value({"path": "state.sqlite3", "namespace": None}) == (
        '{ path = "state.sqlite3" }'
    )


def test_backend_factory_reports_missing_simpy_extra(monkeypatch):
    original_import = builtins.__import__

    def failing_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "sose.backends.simpy":
            raise ModuleNotFoundError("simpy")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    with pytest.raises(RuntimeError, match="requires installing the 'simpy' extra"):
        _backend_factory("simpy")


def test_build_job_from_config_closes_owned_stores_when_job_construction_fails(
    tmp_path,
    monkeypatch,
):
    from sose.jobs import factory as factory_module
    from sose.jobs.config import SOSEConfig

    class _ClosingStore:
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1
            raise RuntimeError("close failed")

    persistence = _ClosingStore()
    warehouse = _ClosingStore()

    class _StoragePlan:
        healthy = True

        def create_authoritative(self, *, registry, base_dir):
            return persistence

        def create_sink_bindings(self, *, registry, base_dir):
            return ()

    class _BrokenJob:
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("boom")

    config = SOSEConfig.model_validate(
        {
            "domain": {"name": "tutorial_job"},
            "persistence": {"adapter": "memory"},
            "job": {"id": "factory-cleanup"},
        }
    )

    monkeypatch.setattr(factory_module, "build_storage_plan", lambda *a, **k: _StoragePlan())
    monkeypatch.setattr(factory_module, "_domain_warehouse", lambda *a, **k: warehouse)
    monkeypatch.setattr(factory_module, "SimulationJob", _BrokenJob)

    with pytest.raises(RuntimeError, match="boom"):
        build_job_from_config(config, base_dir=Path(tmp_path))

    assert persistence.closed == 1
    assert warehouse.closed == 1
