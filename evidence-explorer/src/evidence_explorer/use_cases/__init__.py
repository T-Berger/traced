"""Portable purpose plans and pure projections over existing evidence review."""

from .alerts import evaluate_activity_alert, illustrative_activity_example
from .catalog import get_use_case, list_use_cases
from .contracts import (
    ActivityWindow,
    AlertEvaluation,
    AlertRule,
    BriefBindingFact,
    CaseProgress,
    DossierReadiness,
    MonitoringBrief,
    PolicyReference,
    ProgressFacts,
    ReadinessFact,
    WindowDefinition,
)
from .process import process_definition, project_case_progress
from .reporting import platform_reporting_references, project_dossier_readiness

__all__ = [
    "ActivityWindow",
    "AlertEvaluation",
    "AlertRule",
    "BriefBindingFact",
    "CaseProgress",
    "DossierReadiness",
    "MonitoringBrief",
    "PolicyReference",
    "ProgressFacts",
    "ReadinessFact",
    "WindowDefinition",
    "evaluate_activity_alert",
    "get_use_case",
    "illustrative_activity_example",
    "list_use_cases",
    "platform_reporting_references",
    "process_definition",
    "project_case_progress",
    "project_dossier_readiness",
]
