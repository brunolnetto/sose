"""Testing helpers for durable reference-domain evidence."""

from .conformance import (
    BASELINE_REFERENCE_CAPABILITIES,
    CAPABILITY_REQUIREMENTS,
    ReferenceCapability,
    ReferenceConformanceIssue,
    ReferenceContract,
    validate_reference_catalog,
    validate_reference_contract,
)
from .restart import ReferenceRuntime, restart_reference_runtime

__all__ = [
    "BASELINE_REFERENCE_CAPABILITIES",
    "CAPABILITY_REQUIREMENTS",
    "ReferenceCapability",
    "ReferenceConformanceIssue",
    "ReferenceContract",
    "ReferenceRuntime",
    "restart_reference_runtime",
    "validate_reference_catalog",
    "validate_reference_contract",
]
