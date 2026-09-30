from sose.persistence.registry import builtin_persistence_registry


def test_ducklake_is_registered_as_durable_candidate():
    capabilities = builtin_persistence_registry().capabilities("ducklake")

    assert capabilities.process_durable is True
    assert capabilities.transactional_commits is True
    assert capabilities.incremental_updates is True
    assert capabilities.concurrent_writers is True
    assert capabilities.analytical_reads is True
    assert capabilities.authoritative_read_after_commit is True
    assert capabilities.restart_reconstructible is True

    # Promotion remains evidence-gated.
    assert capabilities.conditional_writes is False
    assert capabilities.durable_job_leases is False
    assert capabilities.fencing is False
