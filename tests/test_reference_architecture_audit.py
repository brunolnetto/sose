from pathlib import Path

from tests.support.reference_catalog import REFERENCE_CATALOG


REPO_ROOT = Path(__file__).resolve().parents[1]

BANNED_REFERENCE_SOURCE_PATTERNS = {
    "def _request_resource(": "use DurableResourceManager.ensure_requested()",
    "def _release_resource(": "use DurableResourceManager.withdraw()",
    "def _resource_request_exists(": "use DurableResourceManager.has_request()",
    "def _reservation_for(": "use manager.reservation_for()",
    "def _preemptive_request_exists(": "use DurablePreemptiveResourceManager.has_request()",
    "def _preemptive_reservation(": "use preemptive manager.reservation_for()",
    "def _resource_reservation(": "use resource manager.reservation_for()",
    "engine.resources.cancel_pending(backend": "use DurableResourceManager.withdraw()",
    "engine.preemptive_resources.cancel_pending(backend": (
        "use DurablePreemptiveResourceManager.withdraw()"
    ),
    "engine.stores.result(": "use DurableStoreManager.selection()/ensure_selection()",
    "persistence.scheduled_work()": "use DurableScheduler lifecycle APIs",
}


def _package_dir(package: str) -> Path:
    return REPO_ROOT / "src" / Path(*package.split("."))


def test_reference_sources_do_not_reimplement_promoted_runtime_lifecycles():
    violations: list[str] = []
    for contract in REFERENCE_CATALOG:
        for path in sorted(_package_dir(contract.package).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for pattern, replacement in BANNED_REFERENCE_SOURCE_PATTERNS.items():
                if pattern in text:
                    relative = path.relative_to(REPO_ROOT)
                    violations.append(
                        f"{contract.domain}: {relative}: {pattern!r} -> {replacement}"
                    )

    assert violations == [], "\n".join(violations)
