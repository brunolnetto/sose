import sose.api as api


EXPECTED_PUBLIC_API = {
    "AttributeEffect",
    "AnalyticalSink",
    "AnalyticalBatch",
    "CompositeEffect",
    "ContainerBackend",
    "DiagnosticIssue",
    "DomainCatalog",
    "DomainConfig",
    "DomainDefinition",
    "DomainRegistry",
    "Engine",
    "Entity",
    "EntityType",
    "EventTrigger",
    "JobDoctorIssue",
    "JobDoctorReport",
    "JobTickResult",
    "JobTriggerResult",
    "JSONLJournalPersistence",
    "MemoryPersistence",
    "Persistence",
    "PersistenceAdapter",
    "PersistenceCapabilities",
    "PersistenceRegistry",
    "PreemptiveResourceBackend",
    "RandomSource",
    "RuntimeCounts",
    "RuntimeDiagnostics",
    "ResourceBackend",
    "SQLiteIncrementalPersistence",
    "SQLitePersistence",
    "SOSEConfig",
    "Scenario",
    "Scheduler",
    "ScheduledTrigger",
    "SimulationClock",
    "SimulationJob",
    "SimulationJobState",
    "SimulationContext",
    "SinkAdapter",
    "SinkBinding",
    "SinkRegistry",
    "StoreBackend",
    "TemporalBackend",
    "TickTrigger",
    "TransitionWeightEffect",
    "UnitOfWork",
    "probabilistic",
    "build_job_from_config",
    "build_job_from_file",
    "builtin_persistence_registry",
    "builtin_sink_registry",
    "collect_runtime_diagnostics",
    "inspect_job_file_health",
    "inspect_job_health",
    "load_sose_config",
    "probabilistic_transitions",
    "scheduled_trigger_id",
    "render_sose_toml",
    "write_sose_toml",
}


def test_public_api_exports_are_explicit_and_stable():
    assert set(api.__all__) == EXPECTED_PUBLIC_API


def test_every_public_api_symbol_is_importable():
    for name in EXPECTED_PUBLIC_API:
        assert getattr(api, name) is not None


def test_public_api_does_not_eagerly_export_optional_simpy_backend():
    assert "SimPyBackend" not in api.__all__


def test_public_api_does_not_export_reference_domains():
    assert not any(name.startswith("Reference") for name in api.__all__)
