"""Cover idempotent scientific Stage-2 publication and fail-closed historical state.

Tests use deterministic in-memory validated-model stubs; no GitHub access.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sose.organizational import prospective_stage2_acquisition_runner_v2 as runner


@pytest.fixture
def staged(tmp_path, monkeypatch):
    paths = {}
    for name in ("freeze", "tranche", "previous", "state", "checkpoint"):
        path = tmp_path / (name + ".json")
        path.write_text("{}", encoding="utf-8")
        paths[name] = path
    freeze = SimpleNamespace(snapshot_hash="baseline", training_state_hash="training")
    state = SimpleNamespace(snapshot_hash="baseline", previous_state_hash="training")
    acquisition = SimpleNamespace(
        to_evidence_snapshot=lambda: SimpleNamespace(snapshot_hash=state.snapshot_hash)
    )
    previous_checkpoint = SimpleNamespace(checkpoint_hash="checkpoint-id")
    def fake_model(o):
        return SimpleNamespace(model_validate_json=lambda _: o)
    monkeypatch.setattr(runner, "ProspectiveModelFreezeArtifactV2", fake_model(freeze))
    monkeypatch.setattr(runner, "GitHubPRAcquisitionSnapshotV2", fake_model(acquisition))
    monkeypatch.setattr(runner, "ProspectiveEvidenceStateV2", fake_model(state))
    monkeypatch.setattr(runner, "ProspectiveStage2CheckpointV2", fake_model(previous_checkpoint))
    monkeypatch.setattr(
        runner, "build_stage2_checkpoint_v2",
        lambda **_: SimpleNamespace(checkpoint_hash="checkpoint-id"),
    )
    result = SimpleNamespace(
        acquisition=SimpleNamespace(canonical_json=lambda: '{"tranche":1}'),
        state=SimpleNamespace(canonical_json=lambda: '{"revision":1}'),
        checkpoint=SimpleNamespace(canonical_json=lambda: '{"checkpoint":1}'),
    )
    monkeypatch.setattr(runner, "advance_stage2_artifacts_v2", lambda **_: result)
    return paths, freeze, state, acquisition, previous_checkpoint, result


def test_stage2_tranche_publication_is_idempotent_and_detects_tampered_artifact(
    tmp_path, staged,
):
    paths, _, _, _, _, result = staged
    kw = {
        "model_freeze_path": paths["freeze"],
        "tranche_acquisition_path": paths["tranche"],
        "previous_acquisition_path": paths["previous"],
        "previous_state_path": paths["state"],
        "cumulative_acquisition_output_path": tmp_path / "out" / "cum.json",
        "state_output_path": tmp_path / "out" / "state.json",
        "checkpoint_output_path": tmp_path / "out" / "checkpoint.json",
    }
    assert runner.run_stage2_tranche_files_v2(**kw) is result
    assert kw["cumulative_acquisition_output_path"].read_text() == '{"tranche":1}\n'
    assert kw["state_output_path"].read_text() == '{"revision":1}\n'
    assert kw["checkpoint_output_path"].read_text() == '{"checkpoint":1}\n'
    assert runner.run_stage2_tranche_files_v2(**kw) is result
    kw["state_output_path"].write_text("tampered\n", encoding="utf-8")
    with pytest.raises(FileExistsError, match="different artifact"):
        runner.run_stage2_tranche_files_v2(**kw)
    assert kw["state_output_path"].read_text() == "tampered\n"


def test_historical_stage2_first_checkpoint_and_later_checkpoint_validation(staged):
    paths, freeze, state, acquisition, previous_checkpoint, _ = staged
    kwargs = dict(model_freeze_path=paths["freeze"],
                  previous_acquisition_path=paths["previous"],
                  previous_state_path=paths["state"], previous_checkpoint_path=None)
    runner._validate_previous_bundle(**kwargs)
    state.snapshot_hash = "later"
    with pytest.raises(ValueError, match="checkpoint is required"):
        runner._validate_previous_bundle(**kwargs)
    runner._validate_previous_bundle(**(kwargs | {"previous_checkpoint_path": paths["checkpoint"]}))
    previous_checkpoint.checkpoint_hash = "changed"
    with pytest.raises(ValueError, match="checkpoint does not match"):
        runner._validate_previous_bundle(**(kwargs | {"previous_checkpoint_path": paths["checkpoint"]}))
    previous_checkpoint.checkpoint_hash = "checkpoint-id"
    state.snapshot_hash = "baseline"
    with pytest.raises(ValueError, match="must not supply a previous checkpoint"):
        runner._validate_previous_bundle(**(kwargs | {"previous_checkpoint_path": paths["checkpoint"]}))
    state.previous_state_hash = "wrong-training"
    with pytest.raises(ValueError, match="exact pre-freeze state"):
        runner._validate_previous_bundle(**kwargs)
    state.previous_state_hash = "training"
    state.snapshot_hash = "later"
    acquisition.to_evidence_snapshot = lambda: SimpleNamespace(snapshot_hash="incorrect")
    with pytest.raises(ValueError, match="same evidence snapshot"):
        runner._validate_previous_bundle(**kwargs)


def test_preflight_rejects_overlaps_existing_outputs_and_conflicting_releases(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    with pytest.raises(ValueError, match="must be distinct"):
        runner._preflight_outputs((a, a))
    with pytest.raises(ValueError, match="must be distinct"):
        runner._preflight_publications(((a, "x"), (a, "x")))
    runner._preflight_outputs((a, b))
    a.write_text("first", encoding="utf-8")
    with pytest.raises(FileExistsError, match="output already exists"):
        runner._preflight_outputs((a, b))
    runner._preflight_publications(((a, "first"), (b, "second")))
    with pytest.raises(FileExistsError, match="different artifact"):
        runner._preflight_publications(((a, "changed"),))


@pytest.mark.parametrize("same", [True, False])
def test_exclusive_publish_cleans_interrupted_tempfile_and_checks_committed_payload(
    tmp_path, monkeypatch, same,
):
    target = tmp_path / "artifact.json"
    recorded = []
    def losing_link(source, destination):
        recorded.append(source)
        Path(destination).write_text("valid" if same else "conflicting", encoding="utf-8")
        raise FileExistsError("lost atomic publication race")
    monkeypatch.setattr(runner.os, "link", losing_link)
    if same:
        runner._publish_exclusive(target, "valid")
        assert target.read_text() == "valid"
    else:
        with pytest.raises(FileExistsError, match="lost atomic publication race"):
            runner._publish_exclusive(target, "valid")
        assert target.read_text() == "conflicting"
    assert recorded
    assert all(not Path(temp).exists() for temp in recorded)
    with pytest.raises(FileExistsError, match="different artifact"):
        runner._publish_exclusive(target, "different")
