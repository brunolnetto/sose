"""Organizational-dynamics modeling layer built on SOSE primitives."""

from .calibration import ObservedItemFlowCalibration, calibrate_observed_item_flow
from .dataset import ObservedPRDataset, ObservedPRSplit
from .experiment import (
    CostAnalysisPlan,
    ExperimentProtocol,
    FalsificationRule,
    MetricDirection,
    MultipleComparisonMethod,
    OutcomeMetric,
    ParameterRange,
    ReplicationPlan,
    SamplingDesign,
    StatisticalPlan,
    SurrogateAnalysisPlan,
    SurrogateModel,
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
from .validation import (
    DistributionSummary,
    LeadTimeValidation,
    LeadTimeValidationAssessment,
    LeadTimeValidationCriteria,
    ValidationCheck,
    assess_lead_time_validation,
    compare_lead_time_distributions,
    empirical_cdf_max_distance,
    summarize_distribution,
)

__all__ = [
    "CostAnalysisPlan",
    "DistributionSummary",
    "EvidenceClass",
    "ExperimentProtocol",
    "FalsificationRule",
    "InterventionClass",
    "LeadTimeValidation",
    "LeadTimeValidationAssessment",
    "LeadTimeValidationCriteria",
    "MetricDirection",
    "ModelIntervention",
    "ModelSpec",
    "MultipleComparisonMethod",
    "ObservedEventKind",
    "ObservedItemFlowCalibration",
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
    "SurrogateAnalysisPlan",
    "SurrogateModel",
    "ValidationCheck",
    "assess_lead_time_validation",
    "calibrate_observed_item_flow",
    "compare_lead_time_distributions",
    "empirical_cdf_max_distance",
    "normalize_github_pr_trace",
    "sample_parameter_space",
    "summarize_distribution",
]
