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
    backend_before=None,
    *,
    backend_factory: BackendFactory,
    tick: int | None = None,
    runtime_kwargs: Mapping[str, object] | None = None,
    drain_boundary: bool = True,
) -> ReferenceRuntime:
    """Rebuild a reference runtime from one durable recovery boundary.

    The boundary comes from backend_before.now when supplied, otherwise from the
    persisted SimulationPosition. The helper standardizes only restart mechanics:
    - resolve the logical recovery boundary;
    - recreate context/engine against the same persistence;
    - reconstruct a fresh backend;
    - drain callbacks exactly at the recovery boundary.

    Domain-specific continuation and equality assertions remain in the caller.
    """

    position = persistence.simulation_position()
    if backend_before is not None:
        restarted_at = backend_before.now
    elif position is not None:
        restarted_at = position.logical_time
    else:
        raise ValueError(
            "restart requires backend_before or a persisted simulation position"
        )

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


def restart_reference_runtime_repeated(
    persistence,
    build_runtime: RuntimeBuilder,
    backend_before,
    *,
    backend_factory: BackendFactory,
    count: int,
    tick: int | None = None,
    runtime_kwargs: Mapping[str, object] | None = None,
    drain_boundary: bool = True,
) -> ReferenceRuntime:
    """Rebuild the same durable boundary repeatedly.

    This helper is a hardening primitive rather than a domain assertion. It is
    useful for detecting reconstruction logic that accidentally depends on
    one-shot backend state or mutates durable truth during rebuild.
    """

    if count < 1:
        raise ValueError("repeated restart count must be >= 1")

    current_backend = backend_before
    result: ReferenceRuntime | None = None
    for _ in range(count):
        result = restart_reference_runtime(
            persistence,
            build_runtime,
            current_backend,
            backend_factory=backend_factory,
            tick=tick,
            runtime_kwargs=runtime_kwargs,
            drain_boundary=drain_boundary,
        )
        current_backend = result.backend

    assert result is not None
    return result
