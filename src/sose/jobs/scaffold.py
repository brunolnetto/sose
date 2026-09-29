from __future__ import annotations

from hashlib import sha256
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

    resolved_job_id = job_id or f"{definition.name}-job"
    persistence_options = [
        "",
        "[persistence]",
        f"adapter = {_toml_value(persistence_adapter)}",
        "",
        "[persistence.options]",
    ]
    if persistence_adapter == "postgres":
        namespace = "".join(
            character
            if (
                "A" <= character <= "Z"
                or "a" <= character <= "z"
                or "0" <= character <= "9"
                or character == "_"
            )
            else "_"
            for character in resolved_job_id
        )
        if not namespace or namespace[0].isdigit():
            namespace = f"job_{namespace}"
        if len(namespace) > 40:
            suffix = sha256(resolved_job_id.encode("utf-8")).hexdigest()[:8]
            namespace = f"{namespace[:31]}_{suffix}"
        persistence_options.extend(
            [
                'dsn_env = "SOSE_DATABASE_URL"',
                f"namespace = {_toml_value(namespace)}",
            ]
        )
    else:
        persistence_options.append(
            f"path = {_toml_value(persistence_path)}"
        )

    lines.extend(
        persistence_options
        + [
            "",
            "[runtime]",
            f"backend = {_toml_value(runtime_backend)}",
            "",
            "[job]",
            f"id = {_toml_value(resolved_job_id)}",
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
