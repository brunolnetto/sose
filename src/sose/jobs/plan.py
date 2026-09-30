from __future__ import annotations

from pathlib import Path

from sose.jobs.config import SOSEConfig, load_sose_config
from sose.jobs.job_edit import describe_job_policy
from sose.jobs.storage import build_storage_plan


def build_execution_plan(
    config: SOSEConfig,
) -> dict[str, object]:
    from sose.examples.catalog import builtin_catalog

    definition = builtin_catalog().get(config.domain.name)
    resolved = definition.parse_config(config.domain.parameters)
    storage = build_storage_plan(config)

    return {
        "domain": {
            "name": definition.name,
            "description": definition.description,
            "config_model": definition.config_model.__name__,
            "parameters": resolved.model_dump(mode="json"),
            "runtime_mutable_fields": sorted(
                definition.runtime_mutable_fields
            ),
        },
        "storage": storage.describe(),
        "runtime": {
            "backend": config.runtime.backend,
        },
        "job": {
            "id": config.job.id,
            "ticks_per_trigger": config.job.ticks_per_trigger,
            "max_ticks_per_trigger": config.job.max_ticks_per_trigger,
            "execution_model": "durable_recurring_trigger",
            "external_scheduler_owned": True,
        },
    }


def build_execution_plan_from_file(
    path: str | Path = "sose.toml",
) -> dict[str, object]:
    config, _ = load_sose_config(path)
    return build_execution_plan(config)
