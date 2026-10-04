from __future__ import annotations

import pytest

from sose.jobs.storage_edit import (
    _append_regular_section,
    _find_sink_block,
    _first_top_level_section_index,
    _remove_sink_block,
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


def test_add_sink_appends_when_job_is_inline_table(tmp_path):
    path = tmp_path / "sose.toml"
    path.write_text(
        """
domain = { name = "tutorial_job" }
job = { id = "inline-job" }
""".strip(),
        encoding="utf-8",
    )

    result = add_analytical_sink(
        path,
        name="warehouse",
        adapter="jsonl",
    )

    assert result["name"] == "warehouse"
    text = path.read_text(encoding="utf-8")
    assert text.rstrip().endswith(
        'job = { id = "inline-job" }\n\n[[sinks]]\nname = "warehouse"\nadapter = "jsonl"'
    )


def test_add_sink_inserts_blank_between_sink_and_suffix_section(tmp_path):
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

    add_analytical_sink(path, name="warehouse", adapter="jsonl")
    text = path.read_text(encoding="utf-8")
    assert 'adapter = "jsonl"\n\n[job]\nid = "demo"' in text


def test_remove_sink_repairs_spacing_when_block_is_inline_with_sections(tmp_path):
    path = tmp_path / "sose.toml"
    path.write_text(
        """
[domain]
name = "tutorial_job"
[[sinks]]
name = "warehouse"
adapter = "jsonl"
[domain.parameters]
auto_complete = true
[job]
id = "demo"
""".strip(),
        encoding="utf-8",
    )

    remove_analytical_sink(path, name="warehouse")
    text = path.read_text(encoding="utf-8")
    assert 'name = "tutorial_job"\n\n[domain.parameters]' in text


def test_replace_regular_section_replaces_terminal_section_without_extra_blank():
    assert _replace_regular_section(
        ["[engine_store]", 'adapter = "memory"'],
        "[engine_store]",
        ['adapter = "sqlite_incremental"'],
    ) == [
        "[engine_store]",
        'adapter = "sqlite_incremental"',
    ]


def test_find_remove_and_append_section_helpers_cover_spacing_edges():
    lines = [
        "[[sinks]]",
        'name = "first"',
        'adapter = "jsonl"',
        "",
        "[[sinks]]",
        'name = "second"',
        'adapter = "jsonl"',
        "",
        "[domain]",
        'name = "tutorial_job"',
    ]
    blocks = _sink_blocks(lines)

    assert _find_sink_block(lines, blocks, name="missing") is None

    updated = _remove_sink_block(lines, (0, 4))
    assert updated[0] == "[[sinks]]"
    assert updated[1] == 'name = "second"'

    appended = _append_regular_section(
        ["[domain]", 'name = "tutorial_job"'],
        "[job]",
        ['id = "demo"'],
    )
    assert appended == [
        "[domain]",
        'name = "tutorial_job"',
        "",
        "[job]",
        'id = "demo"',
    ]


def test_remove_sink_block_trims_following_blank_rows_and_append_preserves_blank_tail():
    updated = _remove_sink_block(
        ["[[sinks]]", 'name = "x"', "", "", "[domain]"],
        (0, 2),
    )
    assert updated[0] == "[domain]"

    preserved = _append_regular_section(
        ["[domain]", 'name = "tutorial_job"', ""],
        "[job]",
        ['id = "demo"'],
    )
    assert preserved == [
        "[domain]",
        'name = "tutorial_job"',
        "",
        "[job]",
        'id = "demo"',
    ]
