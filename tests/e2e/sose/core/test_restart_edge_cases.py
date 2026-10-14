import pytest

from sose.core.runtime import SimulationPosition
from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import (
    restart_reference_runtime,
    restart_reference_runtime_repeated,
)


RESTART_OWNER = "restart-edge-case"


def test_restart_reference_runtime_requires_backend_or_position():
    persistence = MemoryPersistence()

    def _unused_builder(*args, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("builder must not be called")

    with pytest.raises(
        ValueError,
        match="restart requires backend_before or a persisted simulation position",
    ):
        restart_reference_runtime(
            persistence,
            _unused_builder,
            owner_id=RESTART_OWNER,
            backend_factory=lambda origin: object(),
        )


def test_restart_reference_runtime_repeated_rejects_zero_count():
    persistence = MemoryPersistence()

    def _unused_builder(*args, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("builder must not be called")

    with pytest.raises(
        ValueError,
        match="repeated restart count must be >= 1",
    ):
        restart_reference_runtime_repeated(
            persistence,
            _unused_builder,
            backend_before=object(),
            owner_id=RESTART_OWNER,
            backend_factory=lambda origin: object(),
            count=0,
        )


def test_restart_reference_runtime_can_skip_boundary_drain():
    persistence = MemoryPersistence()
    position = SimulationPosition(
        logical_time=1,
        logical_tick=2,
        execution_sequence=0,
        committed_sequence=0,
    )
    with persistence.transaction() as uow:
        uow.set_simulation_position(position, owner_id=RESTART_OWNER)

    class _Backend:
        def __init__(self, origin):
            self.origin = origin
            self.run_until_calls = []

        def run_until(self, when):
            self.run_until_calls.append(when)

    class _Engine:
        def __init__(self):
            self.backend = None

        def rebuild_backend(self, backend):
            self.backend = backend

    def _build_runtime(*_args, owner_id, **_kwargs):
        assert owner_id == RESTART_OWNER
        return object(), _Engine()

    result = restart_reference_runtime(
        persistence,
        _build_runtime,
        backend_before=None,
        owner_id=RESTART_OWNER,
        backend_factory=lambda origin: _Backend(origin),
        drain_boundary=False,
    )

    assert result.backend.run_until_calls == []
