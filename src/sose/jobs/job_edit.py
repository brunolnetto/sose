from __future__ import annotations

from pathlib import Path
import re

from sose.jobs.config import JobSection, load_sose_config
from sose.jobs.scaffold import _toml_value


def describe_job_policy(path: str | Path) -> dict[str, object]:
    config, _ = load_sose_config(path)
    return {
        "job_id": config.job.id,
        "ticks_per_trigger": config.job.ticks_per_trigger,
        "max_ticks_per_trigger": config.job.max_ticks_per_trigger,
        "execution_model": "durable_recurring_trigger",
        "external_scheduler_owned": True,
    }


def set_job_policy(
    path: str | Path,
    *,
    ticks_per_trigger: int | None = None,
    max_ticks_per_trigger: int | None = None,
) -> dict[str, object]:
    config_path = Path(path)
    config, _ = load_sose_config(config_path)

    desired = JobSection.model_validate(
        {
            "id": config.job.id,
            "ticks_per_trigger": (
                config.job.ticks_per_trigger
                if ticks_per_trigger is None
                else ticks_per_trigger
            ),
            "max_ticks_per_trigger": (
                config.job.max_ticks_per_trigger
                if max_ticks_per_trigger is None
                else max_ticks_per_trigger
            ),
        }
    )

    before = config_path.read_text(encoding="utf-8")
    after = _write_job_section(
        before,
        desired,
    )
    config_path.write_text(after, encoding="utf-8")

    return {
        "job_id": desired.id,
        "ticks_per_trigger": desired.ticks_per_trigger,
        "max_ticks_per_trigger": desired.max_ticks_per_trigger,
        "applied": False,
    }


def _write_job_section(text: str, job: JobSection) -> str:
    lines = text.splitlines()
    start, end = _job_section_bounds(lines)

    replacements = {
        "id": _toml_value(job.id),
        "ticks_per_trigger": _toml_value(job.ticks_per_trigger),
        "max_ticks_per_trigger": _toml_value(job.max_ticks_per_trigger),
    }
    seen = _rewrite_existing_job_assignments(lines, start=start, end=end, replacements=replacements)
    _insert_missing_job_assignments(
        lines,
        start=start,
        end=end,
        replacements=replacements,
        seen=seen,
    )
    return "\n".join(lines).rstrip() + "\n"


def _job_section_bounds(lines: list[str]) -> tuple[int, int]:
    header = "[job]"
    start = next(
        (index for index, line in enumerate(lines) if line.strip() == header),
        None,
    )
    if start is None:
        raise ValueError("SOSE config is missing [job] section")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break
    return start, end


def _rewrite_existing_job_assignments(
    lines: list[str],
    *,
    start: int,
    end: int,
    replacements: dict[str, str],
) -> set[str]:
    seen: set[str] = set()
    assignment = re.compile(
        r"^(?P<indent>\s*)(?P<key>id|ticks_per_trigger|max_ticks_per_trigger)\s*="
    )
    for index in range(start + 1, end):
        match = assignment.match(lines[index])
        if match is None:
            continue
        key = match.group("key")
        indent = match.group("indent")
        lines[index] = f"{indent}{key} = {replacements[key]}"
        seen.add(key)
    return seen


def _insert_missing_job_assignments(
    lines: list[str],
    *,
    start: int,
    end: int,
    replacements: dict[str, str],
    seen: set[str],
) -> None:
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1

    for key in ("id", "ticks_per_trigger", "max_ticks_per_trigger"):
        if key not in seen:
            lines.insert(insert_at, f"{key} = {replacements[key]}")
            insert_at += 1
