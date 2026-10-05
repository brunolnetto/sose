"""Organizational-dynamics modeling layer built on SOSE primitives."""

from .dataset import ObservedPRDataset, ObservedPRSplit
from .experiment import (
    ExperimentProtocol,
    FalsificationRule,
    MetricDirection,
    MultipleComparisonMethod,
    OutcomeMetric,
    ParameterRange,
    ReplicationPlan,
    SamplingDesign,
    StatisticalPlan,
)
from .github_observations import normalize_github_pr_trace
from .model_spec import (
    EvidenceClass,
    InterventionClass,
    ModelIntervention,
    ModelSpec,
    StructuralDiff,
)
from .observations import ObservedEventKind, ObservedPREvent, ObservedPRTrace
from .sampling import sample_parameter_space

__all__ = [
    "EvidenceClass",
    "ExperimentProtocol",
    "FalsificationRule",
    "InterventionClass",
    "MetricDirection",
    "ModelIntervention",
    "ModelSpec",
    "MultipleComparisonMethod",
    "ObservedEventKind",
    "ObservedPRDataset",
    "ObservedPREvent",
    "ObservedPRSplit",
    "ObservedPRTrace",
    "OutcomeMetric",
    "ParameterRange",
    "ReplicationPlan",
    "SamplingDesign",
    "StatisticalPlan",
    "StructuralDiff",
    "normalize_github_pr_trace",
    "sample_parameter_space",
]
