import sose.api as api


EXPECTED_PUBLIC_API = {
    "AttributeEffect",
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
    "JSONLJournalPersistence",
    "MemoryPersistence",
    "Persistence",
    "PreemptiveResourceBackend",
    "RandomSource",
    "RuntimeCounts",
    "RuntimeDiagnostics",
    "ResourceBackend",
    "SQLiteIncrementalPersistence",
    "SQLitePersistence",
    "Scenario",
    "Scheduler",
    "ScheduledTrigger",
    "SimulationClock",
    "SimulationContext",
    "StoreBackend",
    "TemporalBackend",
    "TickTrigger",
    "TransitionWeightEffect",
    "UnitOfWork",
    "probabilistic",
    "collect_runtime_diagnostics",
    "probabilistic_transitions",
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
