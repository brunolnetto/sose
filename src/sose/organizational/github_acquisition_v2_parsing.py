"""Strict GitHub payload field parsers (split from github_acquisition_v2)."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any


def _strict_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _stripped(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _known_and_different(left: object, right: object) -> bool:
    """Both identities are present and they disagree."""
    return left is not None and right is not None and left != right


def _workflow_run_belongs_to_pr(run: Mapping[str, Any], *, pr_number: int) -> bool:
    pull_requests = run.get("pull_requests")
    if not isinstance(pull_requests, list):
        return False
    return any(
        isinstance(pull_request, Mapping) and pull_request.get("number") == pr_number
        for pull_request in pull_requests
    )


def _iso_datetime(value: object, *, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"GitHub source requires {name}")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"GitHub source requires ISO-8601 {name}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"GitHub source requires timezone-aware {name}")
    return parsed


def _require_mapping(value: object, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"GitHub {name} payload must be an object")
    return value


def _positive_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"GitHub {name} must be a positive integer")
    return value


def _non_negative_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"GitHub {name} must be a non-negative integer")
    return value


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.strip()
        if 'rel="next"' not in section:
            continue
        if not section.startswith("<") or ">" not in section:
            raise ValueError("malformed GitHub Link header")
        return section[1 : section.index(">")]
    return None
