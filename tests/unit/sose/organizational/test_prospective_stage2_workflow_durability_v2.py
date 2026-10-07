from pathlib import Path


WORKFLOW = Path(".github/workflows/prospective-stage2-acquisition-v2.yml")


def test_stage2_workflow_persists_canonical_bundle_to_durable_evidence_branch() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "contents: write" in text
    assert "evidence/prospective-v2-stage2" in text
    assert "stage2-output/cumulative-acquisition.json" in text
    assert "stage2-output/prospective-state.json" in text
    assert "stage2-output/stage2-checkpoint.json" in text
    assert "stage2-output/tranche-acquisition.json" in text
    assert "checkpoint-" in text
    assert "latest" in text


def test_stage2_workflow_can_restore_previous_bundle_from_durable_evidence_branch() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "Restore previous Stage-2 bundle from durable evidence branch" in text
    assert "previous/stage2-checkpoint.json" in text
    assert "SOSE_HAS_PREVIOUS" in text
    assert "previous_run_id" in text
