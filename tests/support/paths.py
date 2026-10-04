from __future__ import annotations

from pathlib import Path


def repo_root_from(path: str | Path) -> Path:
    current = Path(path).resolve()
    for candidate in current.parents:
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "src" / "sose"
        ).is_dir():
            return candidate
    raise RuntimeError(f"could not determine repository root from {current}")
