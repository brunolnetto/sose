"""Single place that emits SOSE deprecation warnings (policy: ADR-0011).

Every deprecation names the release that deprecated it, the release that removes it
(the next minor) and the replacement, so callers can act on the message alone.
"""
from __future__ import annotations

import importlib
import warnings
from collections.abc import Callable, Mapping


def deprecated_reexports(
    module_name: str, moved: Mapping[str, str], *, since: str, removed_in: str,
) -> Callable[[str], object]:
    """Build a PEP 562 module ``__getattr__`` for names that moved to another module.

    ``moved`` maps each old attribute name to the module that now defines it. Access
    through the old module (``from old import name`` or ``old.name``) still works but warns.
    """
    moved = dict(moved)

    def __getattr__(name: str) -> object:
        target = moved.get(name)
        if target is None:
            raise AttributeError(f"module {module_name!r} has no attribute {name!r}")
        # helper -> __getattr__ -> the importing frame
        warn_deprecated(
            f"importing {name} from {module_name}", since=since, removed_in=removed_in,
            instead=f"import it from {target}", stacklevel=3,
        )
        return getattr(importlib.import_module(target), name)

    return __getattr__


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
