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
from .synthetic_analysis import (
    AnalyticalWorldReference,
    RegimeTransition,
    SyntheticEffectRecovery,
    SyntheticRecoveryReport,
    SyntheticWorldRecovery,
    analytical_world_reference,
    analyze_reference_synthetic_experiment,
)
from .synthetic_runner import (
    SyntheticExperimentDataset,
    SyntheticItemRecord,
    SyntheticRunResult,
    run_reference_synthetic_experiment,
)
from .synthetic_study import SyntheticWorldSpec, generate_synthetic_worlds
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
    "AnalyticalWorldReference",
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
    "RegimeTransition",
    "ReplicationPlan",
    "SamplingDesign",
    "StatisticalPlan",
    "StructuralDiff",
    "SurrogateAnalysisPlan",
    "SurrogateModel",
    "SyntheticEffectRecovery",
    "SyntheticExperimentDataset",
    "SyntheticItemRecord",
    "SyntheticRecoveryReport",
    "SyntheticRunResult",
    "SyntheticWorldRecovery",
    "SyntheticWorldSpec",
    "ValidationCheck",
    "analytical_world_reference",
    "analyze_reference_synthetic_experiment",
    "assess_lead_time_validation",
    "calibrate_observed_item_flow",
    "compare_lead_time_distributions",
    "empirical_cdf_max_distance",
    "normalize_github_pr_trace",
    "sample_parameter_space",
    "generate_synthetic_worlds",
    "run_reference_synthetic_experiment",
    "summarize_distribution",
]
