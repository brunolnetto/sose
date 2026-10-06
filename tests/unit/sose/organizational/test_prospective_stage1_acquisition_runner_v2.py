from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from sose.organizational.prospective_stage1_acquisition_runner_v2 import run_stage1_acquisition_v2


class _FakeClient:
    pass


def test_orchestration_acquires_then_advances_initial_tranche(tmp_path: Path) -> None:
    protocol = tmp_path / "protocol.json"
    protocol.write_text("{}", encoding="utf-8")
    observed: list[tuple[str, dict[str, object]]] = []

    def acquire(**kwargs: object) -> object:
        observed.append(("acquire", kwargs))
        Path(kwargs["output_path"]).write_text("{\"tranche\":true}\n", encoding="utf-8")
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

    result = run_stage1_acquisition_v2(
        repository="brunolnetto/sose",
        pr_numbers=(302, 303, 304),
        captured_at=datetime(2026, 10, 6, 18, 45, tzinfo=timezone.utc),
        protocol_path=protocol,
        output_dir=tmp_path / "out",
        client=_FakeClient(),
        acquire_batch=acquire,
        advance_files=advance,
    )

    assert result.tranche_path.name == "tranche-acquisition.json"
    assert result.cumulative_acquisition_path.name == "cumulative-acquisition.json"
    assert result.state_path.name == "prospective-state.json"
    assert result.checkpoint_path.name == "stage1-readiness.json"
    assert [name for name, _ in observed] == ["acquire", "advance"]
    assert observed[0][1]["repository"] == "brunolnetto/sose"
    assert observed[0][1]["pr_numbers"] == (302, 303, 304)
    assert observed[0][1]["output_path"] == result.tranche_path
    assert observed[1][1]["tranche_acquisition_path"] == result.tranche_path
    assert observed[1][1]["previous_acquisition_path"] is None
    assert observed[1][1]["previous_state_path"] is None


def test_orchestration_passes_previous_stage1_artifacts_as_a_pair(tmp_path: Path) -> None:
    protocol = tmp_path / "protocol.json"
    protocol.write_text("{}", encoding="utf-8")
    previous_acquisition = tmp_path / "previous-acquisition.json"
    previous_state = tmp_path / "previous-state.json"
    previous_acquisition.write_text("previous acquisition", encoding="utf-8")
    previous_state.write_text("previous state", encoding="utf-8")
    observed: dict[str, object] = {}

    def acquire(**kwargs: object) -> object:
        Path(kwargs["output_path"]).write_text("tranche", encoding="utf-8")
        return object()

    def advance(**kwargs: object) -> object:
        observed.update(kwargs)
        return object()

    run_stage1_acquisition_v2(
        repository="brunolnetto/sose",
        pr_numbers=(318, 320),
        captured_at=datetime(2026, 10, 6, 19, 0, tzinfo=timezone.utc),
        protocol_path=protocol,
        output_dir=tmp_path / "out",
        client=_FakeClient(),
        previous_acquisition_path=previous_acquisition,
        previous_state_path=previous_state,
        acquire_batch=acquire,
        advance_files=advance,
    )

    assert observed["previous_acquisition_path"] == previous_acquisition
    assert observed["previous_state_path"] == previous_state


def test_orchestration_rejects_unpaired_previous_artifacts_before_acquisition(tmp_path: Path) -> None:
    called = False

    def acquire(**_: object) -> object:
        nonlocal called
        called = True
        return object()

    with pytest.raises(ValueError, match="previous acquisition and previous state"):
        run_stage1_acquisition_v2(
            repository="brunolnetto/sose",
            pr_numbers=(302, 303),
            captured_at=datetime(2026, 10, 6, 19, 0, tzinfo=timezone.utc),
            protocol_path=tmp_path / "protocol.json",
            output_dir=tmp_path / "out",
            client=_FakeClient(),
            previous_acquisition_path=tmp_path / "previous.json",
            acquire_batch=acquire,
            advance_files=lambda **_: object(),
        )

    assert called is False


def test_orchestration_rejects_missing_previous_artifacts_before_acquisition(tmp_path: Path) -> None:
    protocol = tmp_path / "protocol.json"
    protocol.write_text("{}", encoding="utf-8")
    previous_acquisition = tmp_path / "missing-acquisition.json"
    previous_state = tmp_path / "missing-state.json"
    called = False

    def acquire(**_: object) -> object:
        nonlocal called
        called = True
        return object()

    with pytest.raises(FileNotFoundError, match="previous Stage-1 artifact"):
        run_stage1_acquisition_v2(
            repository="brunolnetto/sose",
            pr_numbers=(318, 320),
            captured_at=datetime(2026, 10, 6, 19, 0, tzinfo=timezone.utc),
            protocol_path=protocol,
            output_dir=tmp_path / "out",
            client=_FakeClient(),
            previous_acquisition_path=previous_acquisition,
            previous_state_path=previous_state,
            acquire_batch=acquire,
            advance_files=lambda **_: object(),
        )

    assert called is False


def test_orchestration_rejects_naive_capture_time_before_network_access(tmp_path: Path) -> None:
    called = False

    def acquire(**_: object) -> object:
        nonlocal called
        called = True
        return object()

    with pytest.raises(ValueError, match="timezone-aware"):
        run_stage1_acquisition_v2(
            repository="brunolnetto/sose",
            pr_numbers=(302, 303),
            captured_at=datetime(2026, 10, 6, 19, 0),
            protocol_path=tmp_path / "protocol.json",
            output_dir=tmp_path / "out",
            client=_FakeClient(),
            acquire_batch=acquire,
            advance_files=lambda **_: object(),
        )

    assert called is False


def test_orchestration_refuses_existing_output_directory_files_before_acquisition(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "tranche-acquisition.json").write_text("old", encoding="utf-8")
    called = False

    def acquire(**_: object) -> object:
        nonlocal called
        called = True
        return object()

    with pytest.raises(FileExistsError, match="tranche-acquisition.json"):
        run_stage1_acquisition_v2(
            repository="brunolnetto/sose",
            pr_numbers=(302, 303),
            captured_at=datetime(2026, 10, 6, 19, 0, tzinfo=timezone.utc),
            protocol_path=tmp_path / "protocol.json",
            output_dir=out,
            client=_FakeClient(),
            acquire_batch=acquire,
            advance_files=lambda **_: object(),
        )

    assert called is False
