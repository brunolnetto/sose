"""Single place that emits SOSE deprecation warnings (policy: ADR-0011).

Every deprecation names the release that deprecated it, the release that removes it
(the next minor) and the replacement, so callers can act on the message alone.
"""
from __future__ import annotations

import warnings


def warn_deprecated(
    what: str, *, since: str, removed_in: str, instead: str, stacklevel: int = 3,
) -> None:
    """Emit a ``DeprecationWarning`` attributed to the caller's caller by default."""
    warnings.warn(
        f"{what} is deprecated since SOSE {since} and will be removed in "
        f"SOSE {removed_in}; {instead}.",
        DeprecationWarning,
        stacklevel=stacklevel,
    )
