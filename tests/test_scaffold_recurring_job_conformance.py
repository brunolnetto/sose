from __future__ import annotations

import pytest

from sose.examples.catalog import builtin_catalog
from sose.jobs.config import load_sose_config
from sose.jobs.config_edit import set_domain_parameter
from sose.jobs.factory import build_job_from_file
from sose.jobs.scaffold import render_sose_toml
from tests.support.domain_config import (
    alternate_runtime_value,
    domain_specific_runtime_fields,
)


def _close(job) -> None:
    close = getattr(job.persistence, "close", None)
    if callable(close):
        close()


@pytest.mark.parametrize("domain_name", builtin_catalog().names())
def test_generated_config_runs_reopens_applies_and_resumes_every_domain(
    domain_name,
    tmp_path,
):
    definition = builtin_catalog().get(domain_name)
    domain_fields = domain_specific_runtime_fields(definition)
    assert domain_fields, domain_name

    config_path = tmp_path / "sose.toml"
    state_path = f"state/{domain_name}.sqlite3"
    config_path.write_text(
        render_sose_toml(
            definition,
            job_id=f"{domain_name}-scaffold-conformance",
            persistence_adapter="sqlite_incremental",
            persistence_path=state_path,
        ),
        encoding="utf-8",
    )

    first_job = build_job_from_file(config_path)
    try:
        first = first_job.run_trigger(
            trigger_id=f"{domain_name}:scaffold:1",
        )
        first_state = first_job.state()
        assert first.config_revision == 1
        assert first.end_tick == 1
        assert first_state is not None
        assert first_state.config_revision == 1
        assert first_state.next_tick == 1
    finally:
        _close(first_job)

    desired, _ = load_sose_config(config_path)
    current_config = definition.parse_config(desired.domain.parameters)
    field_name = domain_fields[0]
    alternate = alternate_runtime_value(
        definition,
        current_config,
        field_name,
    )
    edit = set_domain_parameter(
        config_path,
        name=field_name,
        value=alternate,
    )
    assert edit["mutability"] == "runtime"
    assert edit["applied"] is False

    second_job = build_job_from_file(config_path)
    try:
        before_apply = second_job.state()
        assert before_apply is not None
        assert before_apply.config_revision == 1
        assert before_apply.next_tick == 1

        desired, _ = load_sose_config(config_path)
        applied = second_job.apply_config(desired.domain.parameters)
        assert applied.config_revision == 2

        second = second_job.run_trigger(
            trigger_id=f"{domain_name}:scaffold:2",
        )
        final = second_job.state()

        assert second.config_revision == 2
        assert second.start_tick == 1
        assert second.end_tick == 2
        assert final is not None
        assert final.config_revision == 2
        assert final.next_tick == 2
        assert final.run_count == 2
        assert final.status == "ready"
        assert final.phase == "idle"
    finally:
        _close(second_job)
