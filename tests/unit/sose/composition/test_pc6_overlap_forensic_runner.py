"""An intermittent failure's evidence must survive later diagnostic invocations."""
from scripts.reproduce_pc6_pg_overlap import create_invocation_directory


def test_distinct_invocations_cannot_overwrite_previous_causal_failure(tmp_path):
    first = create_invocation_directory(tmp_path)
    (first / "run-001.log").write_text("FIRST CAUSAL FAILURE", encoding="utf-8")

    second = create_invocation_directory(tmp_path)
    (second / "run-001.log").write_text("SECOND CAUSAL FAILURE", encoding="utf-8")

    assert first != second
    assert first.parent == second.parent == tmp_path
    assert (first / "run-001.log").read_text(encoding="utf-8") == "FIRST CAUSAL FAILURE"
    assert (second / "run-001.log").read_text(encoding="utf-8") == "SECOND CAUSAL FAILURE"
