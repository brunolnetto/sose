from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from sose.jobs.config import load_sose_config
from sose.jobs.config_edit import parse_cli_value
from sose.jobs.scaffold import _toml_value
from sose.jobs.storage import build_storage_plan


def parse_key_value_options(values: list[str] | tuple[str, ...]) -> dict[str, object]:
    result: dict[str, object] = {}
    for item in values:
        key, sep, raw = item.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(
                f"storage option must use KEY=VALUE syntax: {item!r}"
            )
        if key in result:
            raise ValueError(f"duplicate storage option: {key}")
        result[key] = parse_cli_value(raw)
    return result


def set_authoritative_storage(
    path: str | Path,
    *,
    adapter: str,
    options: dict[str, object] | None = None,
    require: tuple[str, ...] = (),
) -> dict[str, object]:
    config_path = Path(path)
    config, _ = load_sose_config(config_path)

    payload = config.model_dump(mode="python")
    payload["engine_store"] = {
        "adapter": adapter,
        "require": list(require),
        "options": dict(options or {}),
    }
    validated = type(config).model_validate(payload)
    plan = build_storage_plan(validated)

    lines = config_path.read_text(encoding="utf-8").splitlines()
    lines = _replace_regular_section(
        lines,
        "[engine_store]",
        [
            f"adapter = {_toml_value(adapter)}",
            f"require = {_toml_value(list(require))}",
        ],
        aliases=("[persistence]",),
    )
    lines = _replace_regular_section(
        lines,
        "[engine_store.options]",
        [
            f"{key} = {_toml_value(value)}"
            for key, value in sorted((options or {}).items())
        ],
        aliases=("[persistence.options]",),
    )
    _write_lines(config_path, lines)

    return {
        "role": "authoritative",
        "adapter": adapter,
        "required_capabilities": list(require),
        "options": dict(options or {}),
        "storage_plan": plan.describe(),
        "applied": False,
    }


def add_analytical_sink(
    path: str | Path,
    *,
    name: str,
    adapter: str,
    options: dict[str, object] | None = None,
) -> dict[str, object]:
    if not name:
        raise ValueError("sink name cannot be empty")

    config_path = Path(path)
    config, _ = load_sose_config(config_path)
    if any(sink.name == name for sink in config.sinks):
        raise ValueError(f"analytical sink already exists: {name}")

    payload = config.model_dump(mode="python")
    payload["sinks"] = [
        *payload.get("sinks", []),
        {
            "name": name,
            "adapter": adapter,
            "options": dict(options or {}),
        },
    ]
    validated = type(config).model_validate(payload)
    plan = build_storage_plan(validated)

    lines = config_path.read_text(encoding="utf-8").splitlines()
    insertion = [
        "[[sinks]]",
        f"name = {_toml_value(name)}",
        f"adapter = {_toml_value(adapter)}",
    ]
    if options:
        insertion.extend(
            [
                "",
                "[sinks.options]",
                *[
                    f"{key} = {_toml_value(value)}"
                    for key, value in sorted(options.items())
                ],
            ]
        )

    insert_at = _first_top_level_section_index(
        lines,
        preferred=("runtime", "job"),
    )
    if insert_at is None:
        insert_at = len(lines)

    prefix = lines[:insert_at]
    suffix = lines[insert_at:]
    if prefix and prefix[-1].strip():
        prefix.append("")
    prefix.extend(insertion)
    if suffix and (not prefix or prefix[-1].strip()):
        prefix.append("")
    _write_lines(config_path, prefix + suffix)

    return {
        "role": "analytical",
        "name": name,
        "adapter": adapter,
        "options": dict(options or {}),
        "storage_plan": plan.describe(),
        "applied": False,
    }


def remove_analytical_sink(
    path: str | Path,
    *,
    name: str,
) -> dict[str, object]:
    config_path = Path(path)
    config, _ = load_sose_config(config_path)
    if not any(sink.name == name for sink in config.sinks):
        raise KeyError(f"unknown analytical sink: {name}")

    lines = config_path.read_text(encoding="utf-8").splitlines()
    blocks = _sink_blocks(lines)

    target: tuple[int, int] | None = None
    for start, end in blocks:
        block_name = _sink_name(lines[start:end])
        if block_name == name:
            target = (start, end)
            break

    if target is None:
        raise RuntimeError(
            f"could not locate analytical sink block in TOML: {name}"
        )

    start, end = target
    updated_lines = lines[:start] + lines[end:]
    while start < len(updated_lines) and not updated_lines[start].strip():
        del updated_lines[start]
    if start > 0 and start < len(updated_lines):
        if updated_lines[start - 1].strip() and updated_lines[start].strip():
            updated_lines.insert(start, "")

    _write_lines(config_path, updated_lines)
    updated_config, _ = load_sose_config(config_path)
    plan = build_storage_plan(updated_config)

    return {
        "role": "analytical",
        "name": name,
        "removed": True,
        "storage_plan": plan.describe(),
        "applied": False,
    }


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _replace_regular_section(
    lines: list[str],
    header: str,
    body: list[str],
    *,
    aliases: tuple[str, ...] = (),
) -> list[str]:
    accepted = {header, *aliases}
    try:
        start = next(
            index
            for index, line in enumerate(lines)
            if line.strip() in accepted
        )
    except StopIteration:
        if lines and lines[-1].strip():
            lines = [*lines, ""]
        return [*lines, header, *body]

    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break

    replacement = [header, *body]
    if end < len(lines) and replacement[-1:]:
        replacement.append("")
    return lines[:start] + replacement + lines[end:]


def _first_top_level_section_index(
    lines: list[str],
    *,
    preferred: tuple[str, ...],
) -> int | None:
    wanted = {f"[{name}]" for name in preferred}
    for index, line in enumerate(lines):
        if line.strip() in wanted:
            return index
    return None


def _sink_blocks(lines: list[str]) -> list[tuple[int, int]]:
    starts = [
        index
        for index, line in enumerate(lines)
        if line.strip() == "[[sinks]]"
    ]
    blocks: list[tuple[int, int]] = []
    for position, start in enumerate(starts):
        next_sink = starts[position + 1] if position + 1 < len(starts) else None
        limit = len(lines) if next_sink is None else next_sink

        end = limit
        for index in range(start + 1, limit):
            stripped = lines[index].strip()
            if (
                stripped.startswith("[")
                and stripped.endswith("]")
                and stripped not in {"[sinks.options]"}
            ):
                end = index
                break
        blocks.append((start, end))
    return blocks


def _sink_name(block: list[str]) -> str | None:
    assignment = re.compile(r'^\s*name\s*=\s*"(?P<name>.*)"\s*$')
    for line in block:
        match = assignment.match(line)
        if match is not None:
            return match.group("name")
    return None
