from __future__ import annotations

import pytest

from sose.jobs.storage_edit import (
    _first_top_level_section_index,
    _replace_regular_section,
    _sink_blocks,
    _sink_name,
    add_analytical_sink,
    parse_key_value_options,
    remove_analytical_sink,
)


def test_parse_key_value_options_rejects_malformed_and_duplicate_keys():
    with pytest.raises(ValueError, match="KEY=VALUE"):
        parse_key_value_options(["missing-separator"])

    with pytest.raises(ValueError, match="KEY=VALUE"):
        parse_key_value_options([" =1"])

    with pytest.raises(ValueError, match="duplicate storage option"):
        parse_key_value_options(["path=a", "path=b"])


def test_parse_key_value_options_parses_cli_scalars():
    assert parse_key_value_options(
        ["enabled=true", "count=3", "ratio=1.5", "name=warehouse"]
    ) == {
        "enabled": True,
        "count": 3,
        "ratio": 1.5,
        "name": "warehouse",
    }


def test_add_sink_rejects_empty_name_before_touching_file(tmp_path):
    path = tmp_path / "missing.toml"

    with pytest.raises(ValueError, match="sink name cannot be empty"):
        add_analytical_sink(path, name="", adapter="jsonl")

    assert not path.exists()


def test_replace_regular_section_appends_when_missing():
    assert _replace_regular_section(
        ["[domain]", 'name = "tutorial_job"'],
        "[engine_store]",
        ['adapter = "memory"'],
    ) == [
        "[domain]",
        'name = "tutorial_job"',
        "",
        "[engine_store]",
        'adapter = "memory"',
    ]


def test_replace_regular_section_replaces_alias_and_preserves_next_section_spacing():
    lines = [
        "[persistence]",
        'adapter = "memory"',
        "[job]",
        'id = "demo"',
    ]

    assert _replace_regular_section(
        lines,
        "[engine_store]",
        ['adapter = "sqlite_incremental"'],
        aliases=("[persistence]",),
    ) == [
        "[engine_store]",
        'adapter = "sqlite_incremental"',
        "",
        "[job]",
        'id = "demo"',
    ]


def test_first_top_level_section_index_returns_preferred_or_none():
    lines = ["[domain]", "[runtime]", "[job]"]

    assert _first_top_level_section_index(lines, preferred=("runtime", "job")) == 1
    assert _first_top_level_section_index(lines, preferred=("missing",)) is None


def test_sink_blocks_stop_at_next_sink_and_unrelated_section():
    lines = [
        "[[sinks]]",
        'name = "a"',
        "[sinks.options]",
        'path = "a.jsonl"',
        "",
        "[[sinks]]",
        'name = "b"',
        "[runtime]",
        'backend = "simpy"',
    ]

    assert _sink_blocks(lines) == [(0, 5), (5, 7)]


def test_sink_name_returns_name_or_none():
    assert _sink_name(['name = "warehouse"']) == "warehouse"
    assert _sink_name(["adapter = \"jsonl\""]) is None


def test_remove_sink_reports_unknown_name(tmp_path):
    path = tmp_path / "sose.toml"
    path.write_text(
        """
[domain]
name = "tutorial_job"

[persistence]
adapter = "memory"

[job]
id = "demo"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(KeyError, match="unknown analytical sink"):
        remove_analytical_sink(path, name="missing")


def test_remove_sink_detects_configured_sink_without_toml_block(tmp_path):
    path = tmp_path / "sose.toml"
    path.write_text(
        """
sinks = [{ name = "warehouse", adapter = "jsonl" }]

[domain]
name = "tutorial_job"

[persistence]
adapter = "memory"

[job]
id = "demo"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="could not locate analytical sink block"):
        remove_analytical_sink(path, name="warehouse")
