from sose.persistence.registry import builtin_persistence_registry


def test_clickhouse_starts_as_analytical_only():
    capabilities = builtin_persistence_registry().capabilities("clickhouse")

    assert capabilities.remote is True
    assert capabilities.analytical_reads is True
    assert capabilities.concurrent_writers is True

    assert capabilities.process_durable is False
    assert capabilities.transactional_commits is False
    assert capabilities.restart_reconstructible is False
    assert capabilities.conditional_writes is False
    assert capabilities.durable_job_leases is False
    assert capabilities.fencing is False
