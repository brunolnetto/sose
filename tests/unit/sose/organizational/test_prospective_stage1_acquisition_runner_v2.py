from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from sose.organizational.prospective_stage1_acquisition_runner_v2 import run_stage1_acquisition_v2


REGISTERED_AT = datetime(2026, 10, 6, 14, 44, 13, tzinfo=timezone.utc)


class _FakeClient:
    pass


def _bind(observed: list[tuple[str, dict[str, object]]]):
    def bind(**kwargs: object) -> object:
        observed.append(("bind", kwargs))
        Path(kwargs["output_path"]).write_text("{\"bound\":true}\n", encoding="utf-8")
        return object()

    return bind


def test_orchestration_binds_then_acquires_then_advances_initial_tranche(tmp_path: Path) -> None:
    protocol = tmp_path / "protocol-document.json"
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
        protocol_document_path=protocol,
        registration_merged_at=REGISTERED_AT,
        output_dir=tmp_path / "out",
        client=_FakeClient(),
        bind_protocol=_bind(observed),
        acquire_batch=acquire,
        advance_files=advance,
    )

    assert result.protocol_path.name == "bound-protocol.json"
    assert result.tranche_path.name == "tranche-acquisition.json"
    assert result.cumulative_acquisition_path.name == "cumulative-acquisition.json"
    assert result.state_path.name == "prospective-state.json"
    assert result.checkpoint_path.name == "stage1-readiness.json"
    assert [name for name, _ in observed] == ["bind", "acquire", "advance"]
    assert observed[0][1]["protocol_document_path"] == protocol
    assert observed[0][1]["registration_merged_at"] == REGISTERED_AT
    assert observed[0][1]["output_path"] == result.protocol_path
    assert observed[1][1]["repository"] == "brunolnetto/sose"
    assert observed[1][1]["pr_numbers"] == (302, 303, 304)
    assert observed[2][1]["protocol_path"] == result.protocol_path
    assert observed[2][1]["tranche_acquisition_path"] == result.tranche_path


def test_orchestration_passes_previous_stage1_artifacts_as_a_pair(tmp_path: Path) -> None:
    protocol = tmp_path / "protocol-document.json"
    protocol.write_text("{}", encoding="utf-8")
    previous_acquisition = tmp_path / "previous-acquisition.json"
    previous_state = tmp_path / "previous-state.json"
    previous_acquisition.write_text("previous acquisition", encoding="utf-8")
    previous_state.write_text("previous state", encoding="utf-8")
    observed: dict[str, object] = {}

    def bind(**kwargs: object) -> object:
        Path(kwargs["output_path"]).write_text("bound", encoding="utf-8")
        return object()

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
        protocol_document_path=protocol,
        registration_merged_at=REGISTERED_AT,
        output_dir=tmp_path / "out",
        client=_FakeClient(),
        previous_acquisition_path=previous_acquisition,
        previous_state_path=previous_state,
        bind_protocol=bind,
        acquire_batch=acquire,
        advance_files=advance,
        validate_previous=lambda *_: None,
    )

    assert observed["previous_acquisition_path"] == previous_acquisition
    assert observed["previous_state_path"] == previous_state


def test_binding_failure_happens_before_acquisition(tmp_path: Path) -> None:
    protocol = tmp_path / "protocol-document.json"
    protocol.write_text("{}", encoding="utf-8")
    acquired = False

    def acquire(**_: object) -> object:
        nonlocal acquired
        acquired = True
        return object()

    def bind(**_: object) -> object:
        raise ValueError("invalid protocol binding")

    with pytest.raises(ValueError, match="invalid protocol binding"):
        run_stage1_acquisition_v2(
            repository="brunolnetto/sose",
            pr_numbers=(302, 303),
            captured_at=datetime(2026, 10, 6, 19, 0, tzinfo=timezone.utc),
            protocol_document_path=protocol,
            registration_merged_at=REGISTERED_AT,
            output_dir=tmp_path / "out",
            client=_FakeClient(),
            bind_protocol=bind,
            acquire_batch=acquire,
            advance_files=lambda **_: object(),
        )

    assert acquired is False


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
            protocol_document_path=tmp_path / "protocol.json",
            registration_merged_at=REGISTERED_AT,
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
            protocol_document_path=protocol,
            registration_merged_at=REGISTERED_AT,
            output_dir=tmp_path / "out",
            client=_FakeClient(),
            previous_acquisition_path=tmp_path / "missing-acquisition.json",
            previous_state_path=tmp_path / "missing-state.json",
            acquire_batch=acquire,
            advance_files=lambda **_: object(),
        )

    assert called is False


def test_orchestration_rejects_naive_timestamps_before_network_access(tmp_path: Path) -> None:
    called = False

    def acquire(**_: object) -> object:
        nonlocal called
        called = True
        return object()

    for captured_at, registered_at in (
        (datetime(2026, 10, 6, 19, 0), REGISTERED_AT),
        (datetime(2026, 10, 6, 19, 0, tzinfo=timezone.utc), datetime(2026, 10, 6, 14, 44, 13)),
    ):
        with pytest.raises(ValueError, match="timezone-aware"):
            run_stage1_acquisition_v2(
                repository="brunolnetto/sose",
                pr_numbers=(302, 303),
                captured_at=captured_at,
                protocol_document_path=tmp_path / "protocol.json",
                registration_merged_at=registered_at,
                output_dir=tmp_path / "out",
                client=_FakeClient(),
                acquire_batch=acquire,
                advance_files=lambda **_: object(),
            )

    assert called is False


def test_orchestration_refuses_existing_output_files_before_acquisition(tmp_path: Path) -> None:
    protocol = tmp_path / "protocol.json"
    protocol.write_text("{}", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "bound-protocol.json").write_text("old", encoding="utf-8")
    called = False

    def acquire(**_: object) -> object:
        nonlocal called
        called = True
        return object()

    with pytest.raises(FileExistsError, match="bound-protocol.json"):
        run_stage1_acquisition_v2(
            repository="brunolnetto/sose",
            pr_numbers=(302, 303),
            captured_at=datetime(2026, 10, 6, 19, 0, tzinfo=timezone.utc),
            protocol_document_path=protocol,
            registration_merged_at=REGISTERED_AT,
            output_dir=out,
            client=_FakeClient(),
            acquire_batch=acquire,
            advance_files=lambda **_: object(),
        )

    assert called is False
