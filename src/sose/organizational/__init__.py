"""Organizational-dynamics modeling layer built on SOSE primitives."""

from .calibration import ObservedItemFlowCalibration, calibrate_observed_item_flow
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
from .heldout_prediction import (
    PRReviewAssumptions,
    PRReviewHeldoutPrediction,
    predict_pr_review_holdout,
)
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
    compare_lead_time_distributions,
    empirical_cdf_max_distance,
    summarize_distribution,
)

__all__ = [
    "DistributionSummary",
    "EvidenceClass",
    "ExperimentProtocol",
    "FalsificationRule",
    "InterventionClass",
    "LeadTimeValidation",
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
    "PRReviewAssumptions",
    "PRReviewHeldoutPrediction",
    "ParameterRange",
    "ReplicationPlan",
    "SamplingDesign",
    "StatisticalPlan",
    "StructuralDiff",
    "calibrate_observed_item_flow",
    "compare_lead_time_distributions",
    "empirical_cdf_max_distance",
    "normalize_github_pr_trace",
    "predict_pr_review_holdout",
    "sample_parameter_space",
    "summarize_distribution",
]
