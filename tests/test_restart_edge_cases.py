import pytest

from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import (
    restart_reference_runtime,
    restart_reference_runtime_repeated,
)


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
            backend_factory=lambda origin: object(),
            count=0,
        )
