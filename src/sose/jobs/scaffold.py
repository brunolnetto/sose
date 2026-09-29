from __future__ import annotations

import json
from pathlib import Path

from sose.domain.config import DomainDefinition


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(
            f"{key} = {_toml_value(item)}"
            for key, item in value.items()
            if item is not None
        ) + " }"
    raise TypeError(f"unsupported TOML scaffold value: {type(value).__name__}")


def render_sose_toml(
    definition: DomainDefinition,
    *,
    job_id: str | None = None,
    persistence_adapter: str = "sqlite_incremental",
    persistence_path: str = "state/sose.sqlite3",
    runtime_backend: str = "simpy",
) -> str:
    defaults = definition.default_config().model_dump(
        mode="json",
        exclude_none=True,
    )

    lines = [
        "[domain]",
        f"name = {_toml_value(definition.name)}",
        "",
        "[domain.parameters]",
    ]
    for key, value in defaults.items():
        lines.append(f"{key} = {_toml_value(value)}")

    lines.extend(
        [
            "",
            "[persistence]",
            f"adapter = {_toml_value(persistence_adapter)}",
            "",
            "[persistence.options]",
            f"path = {_toml_value(persistence_path)}",
            "",
            "[runtime]",
            f"backend = {_toml_value(runtime_backend)}",
            "",
            "[job]",
            f"id = {_toml_value(job_id or f'{definition.name}-job')}",
            "ticks_per_trigger = 1",
            "max_ticks_per_trigger = 100",
            "",
        ]
    )
    return "\n".join(lines)


def write_sose_toml(
    path: str | Path,
    definition: DomainDefinition,
    *,
    job_id: str | None = None,
    persistence_adapter: str = "sqlite_incremental",
    persistence_path: str = "state/sose.sqlite3",
    runtime_backend: str = "simpy",
    force: bool = False,
) -> Path:
    output = Path(path)
    if output.exists() and not force:
        raise FileExistsError(
            f"configuration already exists: {output}; use force to overwrite"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_sose_toml(
            definition,
            job_id=job_id,
            persistence_adapter=persistence_adapter,
            persistence_path=persistence_path,
            runtime_backend=runtime_backend,
        ),
        encoding="utf-8",
    )
    return output
