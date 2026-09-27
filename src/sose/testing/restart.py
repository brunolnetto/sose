from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class RuntimeBuilder(Protocol):
    def __call__(
        self,
        persistence,
        *,
        now,
        tick: int = 0,
        **kwargs: Any,
    ) -> tuple[object, object]: ...


class BackendFactory(Protocol):
    def __call__(self, *, origin) -> object: ...


@dataclass(frozen=True, slots=True)
class ReferenceRuntime:
    """Freshly rebuilt runtime at one durable recovery boundary.

    This helper deliberately contains no domain assertions. Reference tests keep
    ownership of the semantic evidence they compare after continuation.
    """

    context: object
    engine: object
    backend: object
    restarted_at: object
    logical_tick: int


def restart_reference_runtime(
    persistence,
    build_runtime: RuntimeBuilder,
    backend_before,
    *,
    backend_factory: BackendFactory,
    tick: int | None = None,
    runtime_kwargs: Mapping[str, object] | None = None,
    drain_boundary: bool = True,
) -> ReferenceRuntime:
    """Rebuild a reference runtime from the durable state at backend_before.now.

    The helper standardizes only restart mechanics:
    - resolve the logical recovery boundary;
    - recreate context/engine against the same persistence;
    - reconstruct a fresh backend;
    - drain callbacks exactly at the recovery boundary.

    Domain-specific continuation and equality assertions remain in the caller.
    """

    restarted_at = backend_before.now
    position = persistence.simulation_position()
    if tick is None:
        tick = (
            position.logical_tick
            if position is not None and position.logical_time == restarted_at
            else 0
        )

    kwargs = dict(runtime_kwargs or {})
    context, engine = build_runtime(
        persistence,
        now=restarted_at,
        tick=tick,
        **kwargs,
    )
    backend = backend_factory(origin=restarted_at)
    engine.rebuild_backend(backend)
    if drain_boundary:
        backend.run_until(restarted_at)

    return ReferenceRuntime(
        context=context,
        engine=engine,
        backend=backend,
        restarted_at=restarted_at,
        logical_tick=tick,
    )
