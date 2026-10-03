from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from sose.jobs.config import load_sose_config
from sose.jobs.scaffold import _toml_value


def parse_cli_value(raw: str) -> object:
    """Parse a CLI value as JSON when possible, otherwise keep it as text."""

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def describe_domain_config(path: str | Path) -> dict[str, object]:
    from sose.examples.catalog import builtin_catalog

    config, _ = load_sose_config(path)
    definition = builtin_catalog().get(config.domain.name)
    resolved = definition.parse_config(config.domain.parameters)
    description = definition.describe_config()
    current = resolved.model_dump(mode="json")

    parameters: list[dict[str, object]] = []
    for item in description["parameters"]:
        field_name = str(item["field_name"])
        parameters.append(
            {
                **item,
                "value": current[field_name],
            }
        )

    return {
        "domain": definition.name,
        "config_model": definition.config_model.__name__,
        "parameters": parameters,
    }


def set_domain_parameter(
    path: str | Path,
    *,
    name: str,
    value: object,
) -> dict[str, object]:
    """Validate and update one [domain.parameters] value in a SOSE TOML file."""

    from sose.examples.catalog import builtin_catalog

    config_path = Path(path)
    config, _ = load_sose_config(config_path)
    definition = builtin_catalog().get(config.domain.name)
    description = definition.describe_config()

    metadata_by_input_name = {
        str(item["name"]): item
        for item in description["parameters"]
    }
    metadata_by_field_name = {
        str(item["field_name"]): item
        for item in description["parameters"]
    }
    metadata = metadata_by_input_name.get(name) or metadata_by_field_name.get(name)
    if metadata is None:
        raise KeyError(
            f"unknown domain parameter for {definition.name}: {name}"
        )

    input_name = str(metadata["name"])
    field_name = str(metadata["field_name"])
    parameters = dict(config.domain.parameters)

    # Remove either spelling so aliases cannot leave conflicting duplicate input.
    parameters.pop(input_name, None)
    parameters.pop(field_name, None)
    parameters[input_name] = value

    resolved = definition.parse_config(parameters)
    serialized = resolved.model_dump(mode="json")[field_name]
    _write_parameter(config_path, input_name, serialized)

    return {
        "domain": definition.name,
        "name": input_name,
        "field_name": field_name,
        "mutability": metadata["mutability"],
        "value": serialized,
        "applied": False,
    }


def _write_parameter(path: Path, name: str, value: Any) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    header = "[domain.parameters]"
    value_line = f"{name} = {_toml_value(value)}"
    header_index = next(
        (index for index, line in enumerate(lines) if line.strip() == header),
        None,
    )
    if header_index is None:
        _append_domain_parameters_section(lines, header, value_line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    section_end = _section_end(lines, start=header_index)
    updated = _rewrite_parameter_assignment(
        lines,
        start=header_index + 1,
        end=section_end,
        name=name,
        value_line=value_line,
    )
    if not updated:
        _insert_parameter_assignment(
            lines,
            header_index=header_index,
            section_end=section_end,
            value_line=value_line,
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _append_domain_parameters_section(
    lines: list[str],
    header: str,
    value_line: str,
) -> None:
    if lines and lines[-1].strip():
        lines.append("")
    lines.extend([header, value_line])


def _section_end(lines: list[str], *, start: int) -> int:
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            return index
    return len(lines)


def _rewrite_parameter_assignment(
    lines: list[str],
    *,
    start: int,
    end: int,
    name: str,
    value_line: str,
) -> bool:
    assignment = re.compile(rf"^(?P<indent>\s*){re.escape(name)}\s*=")
    for index in range(start, end):
        match = assignment.match(lines[index])
        if match is None:
            continue
        indent = match.group("indent")
        lines[index] = f"{indent}{value_line}"
        return True
    return False


def _insert_parameter_assignment(
    lines: list[str],
    *,
    header_index: int,
    section_end: int,
    value_line: str,
) -> None:
    insert_at = section_end
    while insert_at > header_index + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, value_line)
