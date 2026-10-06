from pathlib import Path


REGISTERED_PROTOCOL_COMMIT = "1cec74738a35ec719d64497164b06c2b39f5e02b"
REGISTERED_PROTOCOL_PATH = "docs/organizational/pr-review-validation-preregistration-v2.json"


def test_stage1_workflow_reads_protocol_from_registered_merge_commit() -> None:
    repository_root = Path(__file__).parents[4]
    workflow = (
        repository_root / ".github/workflows/prospective-stage1-acquisition-v2.yml"
    ).read_text(encoding="utf-8")

    assert "fetch-depth: 0" in workflow
    assert (
        f'git show "{REGISTERED_PROTOCOL_COMMIT}:{REGISTERED_PROTOCOL_PATH}" '
        "> registered-protocol-document.json"
    ) in workflow
    assert 'protocol_document_path=Path("registered-protocol-document.json")' in workflow
    assert (
        'protocol_document_path=Path("docs/organizational/pr-review-validation-preregistration-v2.json")'
        not in workflow
    )
