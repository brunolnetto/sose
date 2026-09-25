from __future__ import annotations

from pathlib import Path

import yaml

from .models import DomainSpec


def load_domain(path: str | Path) -> DomainSpec:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return DomainSpec.model_validate(data)
