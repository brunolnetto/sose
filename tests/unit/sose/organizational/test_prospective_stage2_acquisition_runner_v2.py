from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sose.organizational.prospective_stage2_acquisition_runner_v2 import (
    run_stage2_acquisition_v2,
)


CAPTURED_AT = datetime(2026, 10, 7, 2, 0, tzinfo=UTC)


class _FakeClient:
    pass


def test_stage2_orchestration_validates_before_network_then_acquires_and_advances(
    tmp_path: Path,
) -> None:
    freeze, previous_acquisition, previous_state = _inputs(tmp_path)
    observed: list[tuple[str, dict[str, object]]] = []

    def validate(**kwargs: object) -> object:
        observed.append(("validate", kwargs))
        return object()

    def acquire(**kwargs: object) -> object:
        observed.append(("acquire", kwargs))
        Path(kwargs["output_path"]).write_text("tranche\n", encoding="utf-8")
        return object()

    def advance(**kwargs: object) -> object:
        observed.append(("advance", kwargs))
        for key in (
            "cumulative_acquisition_output_path",
            "state_output_path",
            "checkpoint_output_path",
        ):
            Path(kwargs[key]).write_text(f"{key}\n", encoding="utf-8")
        return object()

    result = run_stage2_acquisition_v2(
        repository="brunolnetto/sose",
        pr_numbers=(330,),
        captured_at=CAPTURED_AT,
        model_freeze_path=freeze,
        previous_acquisition_path=previous_acquisition,
        previous_state_path=previous_state,
        output_dir=tmp_path / "out",
        client=_FakeClient(),
        validate_previous=validate,
        acquire_batch=acquire,
        advance_files=advance,
    )

    assert [name for name, _ in observed] == ["validate", "acquire", "advance"]
    assert observed[1][1]["pr_numbers"] == (330,)
    assert observed[2][1]["model_freeze_path"] == freeze
    assert observed[2][1]["previous_checkpoint_path"] is None
    assert result.tranche_path.name == "tranche-acquisition.json"
    assert result.checkpoint_path.name == "stage2-checkpoint.json"


def test_later_stage2_orchestration_passes_previous_checkpoint(tmp_path: Path) -> None:
    freeze, previous_acquisition, previous_state = _inputs(tmp_path)
    previous_checkpoint = tmp_path / "previous-checkpoint.json"
    previous_checkpoint.write_text("checkpoint\n", encoding="utf-8")
    observed: dict[str, object] = {}

    def acquire(**kwargs: object) -> object:
        Path(kwargs["output_path"]).write_text("tranche\n", encoding="utf-8")
        return object()

    def advance(**kwargs: object) -> object:
        observed.update(kwargs)
        return object()

    run_stage2_acquisition_v2(
        repository="brunolnetto/sose",
        pr_numbers=(331,),
        captured_at=CAPTURED_AT,
        model_freeze_path=freeze,
        previous_acquisition_path=previous_acquisition,
        previous_state_path=previous_state,
        previous_checkpoint_path=previous_checkpoint,
        output_dir=tmp_path / "out",
        client=_FakeClient(),
        validate_previous=lambda **_: object(),
        acquire_batch=acquire,
        advance_files=advance,
    )

    assert observed["previous_checkpoint_path"] == previous_checkpoint


def test_stage2_orchestration_rejects_invalid_previous_bundle_before_network(
    tmp_path: Path,
) -> None:
    freeze, previous_acquisition, previous_state = _inputs(tmp_path)
    acquired = False

    def validate(**_: object) -> object:
        raise ValueError("invalid previous Stage-2 bundle")

    def acquire(**_: object) -> object:
        nonlocal acquired
        acquired = True
        return object()

    with pytest.raises(ValueError, match="invalid previous Stage-2 bundle"):
        run_stage2_acquisition_v2(
            repository="brunolnetto/sose",
            pr_numbers=(330,),
            captured_at=CAPTURED_AT,
            model_freeze_path=freeze,
            previous_acquisition_path=previous_acquisition,
            previous_state_path=previous_state,
            output_dir=tmp_path / "out",
            client=_FakeClient(),
            validate_previous=validate,
            acquire_batch=acquire,
            advance_files=lambda **_: object(),
        )

    assert acquired is False


def test_stage2_orchestration_refuses_existing_outputs_before_network(tmp_path: Path) -> None:
    freeze, previous_acquisition, previous_state = _inputs(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "stage2-checkpoint.json").write_text("old\n", encoding="utf-8")
    acquired = False

    def acquire(**_: object) -> object:
        nonlocal acquired
        acquired = True
        return object()

    with pytest.raises(FileExistsError, match="stage2-checkpoint.json"):
        run_stage2_acquisition_v2(
            repository="brunolnetto/sose",
            pr_numbers=(330,),
            captured_at=CAPTURED_AT,
            model_freeze_path=freeze,
            previous_acquisition_path=previous_acquisition,
            previous_state_path=previous_state,
            output_dir=out,
            client=_FakeClient(),
            validate_previous=lambda **_: object(),
            acquire_batch=acquire,
            advance_files=lambda **_: object(),
        )

    assert acquired is False


def test_stage2_orchestration_rejects_naive_capture_time_before_network(tmp_path: Path) -> None:
    freeze, previous_acquisition, previous_state = _inputs(tmp_path)
    acquired = False

    def acquire(**_: object) -> object:
        nonlocal acquired
        acquired = True
        return object()

    with pytest.raises(ValueError, match="captured_at must be timezone-aware"):
        run_stage2_acquisition_v2(
            repository="brunolnetto/sose",
            pr_numbers=(330,),
            captured_at=datetime(2026, 10, 7, 2, 0),
            model_freeze_path=freeze,
            previous_acquisition_path=previous_acquisition,
            previous_state_path=previous_state,
            output_dir=tmp_path / "out",
            client=_FakeClient(),
            validate_previous=lambda **_: object(),
            acquire_batch=acquire,
            advance_files=lambda **_: object(),
        )

    assert acquired is False


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    freeze = tmp_path / "model-freeze.json"
    previous_acquisition = tmp_path / "previous-acquisition.json"
    previous_state = tmp_path / "previous-state.json"
    for path in (freeze, previous_acquisition, previous_state):
        path.write_text("{}\n", encoding="utf-8")
    return freeze, previous_acquisition, previous_state
