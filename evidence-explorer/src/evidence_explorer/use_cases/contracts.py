"""Portable use-case plans and read-only progress, signal, and dossier records.

These contracts add no scheduler or persistence. A selected template is not a
confirmed monitoring scope, a configured connector is not a verified fetch, and
an activity signal is a request for review rather than a factual verdict.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

StageId = Literal["scope", "collect", "signals", "evidence_check", "human_review", "export"]
StepStatus = Literal["completed", "needs_review", "unavailable", "pending", "in_progress"]
CapabilityStatus = Literal["unknown", "configured", "verified", "unavailable"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class UseCaseDefinition(Contract):
    id: str
    title: str
    target_roles: tuple[str, ...]
    problem: str
    user_story: str
    outcome: str
    scope_notes: tuple[str, ...]
    requires_activity_baseline: bool = False


class WindowDefinition(Contract):
    id: str = Field(min_length=1)
    start: datetime | None = None
    end: datetime | None = None

    @field_validator("start", "end")
    @classmethod
    def timezone_is_explicit(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Window timestamps must include an explicit timezone.")
        return value

    @model_validator(mode="after")
    def ordered_window(self):
        if (
            self.start is not None
            and self.end is not None
            and self.start.astimezone(UTC) >= self.end.astimezone(UTC)
        ):
            raise ValueError("A window must end after it starts.")
        return self


class ActivityWindow(WindowDefinition):
    metric: str = Field(min_length=1)
    source_ids: tuple[str, ...] = Field(min_length=1)
    observed_count: StrictInt | None = Field(default=None, ge=0)
    record_ids: tuple[str, ...] = ()
    timestamp_basis: Literal["publication", "acquisition", "event", "unknown"] = "unknown"
    unknown_time_count: StrictInt = Field(default=0, ge=0)
    coverage_status: Literal["complete", "partial", "unknown"] = "unknown"
    coverage_fraction: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    collection_method: str = Field(min_length=1)
    exposure: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    exposure_unit: str | None = None
    synthetic: StrictBool = False

    @field_validator("coverage_fraction", "exposure", mode="before")
    @classmethod
    def numeric_values_are_not_flags(cls, value):
        if isinstance(value, bool):
            raise ValueError("Numeric measurements cannot be booleans.")
        return value

    @field_validator("source_ids", "record_ids")
    @classmethod
    def unique_nonempty_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not value.strip() for value in values):
            raise ValueError("Source and record identifiers must be nonempty and unique.")
        return values

    @model_validator(mode="after")
    def coverage_is_consistent(self):
        if self.coverage_status == "complete" and self.coverage_fraction not in (None, 1.0):
            raise ValueError("Complete declared coverage cannot have a fraction below one.")
        if self.coverage_status == "unknown" and self.coverage_fraction is not None:
            raise ValueError("Unknown coverage cannot carry an asserted coverage fraction.")
        return self


class AlertRule(Contract):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    threshold_percent: float = Field(gt=0, allow_inf_nan=False)
    threshold_basis: Literal["illustrative_unvalidated", "analyst_configured"]
    direction: Literal["increase", "decrease", "either"]
    comparison_basis: Literal["equal_duration_counts", "exposure_normalized"]
    min_baseline_count: StrictInt = Field(ge=1)
    min_current_count: StrictInt = Field(ge=0)
    min_coverage_fraction: float = Field(gt=0, le=1, allow_inf_nan=False)

    @field_validator("threshold_percent", "min_coverage_fraction", mode="before")
    @classmethod
    def thresholds_are_numbers(cls, value):
        if isinstance(value, bool):
            raise ValueError("Thresholds must be explicit numeric values, not flags.")
        return value


class MonitoringBrief(Contract):
    """An explicit analyst plan, suitable for JSON export without starting collection."""

    id: str = Field(min_length=1)
    use_case_id: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    public_subjects_or_topics: tuple[str, ...] = Field(min_length=1)
    source_selection: tuple[str, ...] = Field(min_length=1)
    reviewer_role: str = Field(min_length=1)
    expected_output: Literal["reviewed_brief", "evidence_dossier"]
    baseline_window: WindowDefinition | None = None
    observation_window: WindowDefinition | None = None
    alert_rule: AlertRule | None = None
    information_scope: Literal["public_information"] = "public_information"
    languages: tuple[str, ...] = ()
    confirmed: StrictBool = False
    note_statistics_status: Literal["unverified_workshop_notes"] = "unverified_workshop_notes"

    @field_validator("purpose", "reviewer_role")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Scope text cannot be blank.")
        return value

    @field_validator("public_subjects_or_topics", "source_selection", "languages")
    @classmethod
    def nonblank_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("Scope values cannot be blank.")
        return values


class ReadinessFact(Contract):
    name: str
    status: CapabilityStatus = "unknown"
    checked_at: datetime | None = None
    evidence: str | None = None

    @model_validator(mode="after")
    def verified_has_evidence(self):
        if self.status == "verified" and (
            self.checked_at is None or not self.evidence or not self.evidence.strip()
        ):
            raise ValueError("Verified readiness requires a check time and supporting evidence.")
        if self.checked_at is not None and (
            self.checked_at.tzinfo is None or self.checked_at.utcoffset() is None
        ):
            raise ValueError("Readiness check time must include a timezone.")
        return self


class AlertCoverage(Contract):
    baseline_fraction: float | None = None
    observation_fraction: float | None = None
    source_ids: tuple[str, ...] = ()
    timestamp_basis: str = "unknown"
    collection_method: str | None = None
    exposure_unit: str | None = None


class AlertEvaluation(Contract):
    rule_id: str
    status: Literal["review_needed", "not_triggered", "unavailable"]
    summary: str
    comparison_basis: str
    threshold_percent: float
    threshold_basis: str
    baseline_value: float | None = None
    observation_value: float | None = None
    relative_change_percent: float | None = None
    record_ids: tuple[str, ...] = ()
    method: str
    coverage: AlertCoverage
    origin: Literal["synthetic", "mixed", "supplied_unknown"] = "supplied_unknown"
    limitations: tuple[str, ...] = ()

    @field_validator("baseline_value", "observation_value", "relative_change_percent")
    @classmethod
    def finite_results(cls, value):
        if value is not None and not math.isfinite(value):
            raise ValueError("An alert result must be finite.")
        return value


class BriefScopeContext(Contract):
    """Compact identity projected from the complete saved case, before display filtering."""

    policy: Literal["saved-evidence-and-scope/v1"] = "saved-evidence-and-scope/v1"
    case_id: str = Field(min_length=1)
    evidence_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_refs: tuple[str, ...]


class BriefBindingFact(Contract):
    """Server projection of a current confirmed revision, not browser authorization.

    Applications construct this from saved revision/confirmation state. The pure
    process projection also checks its case, plan and source identities; a raw
    MonitoringBrief.confirmed flag cannot complete the scope step.
    """

    brief_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    policy: Literal["saved-evidence-and-scope/v1"] = "saved-evidence-and-scope/v1"
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProgressFacts(Contract):
    brief: MonitoringBrief | None = None
    brief_binding: BriefBindingFact | None = None
    collection_state: Literal[
        "unknown", "queued", "running", "completed", "partial", "failed", "unavailable"
    ] = "unknown"
    signal_evaluation: AlertEvaluation | None = None
    model_readiness: ReadinessFact = Field(default_factory=lambda: ReadinessFact(name="Model"))
    platform_readiness: tuple[ReadinessFact, ...] = ()
    exported_assessment_fingerprint: str | None = None


class ProcessStepDefinition(Contract):
    id: StageId
    title: str
    purpose: str
    completion_evidence: str


class StepProgress(Contract):
    id: StageId
    title: str
    status: StepStatus
    summary: str
    record_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


class CaseProgress(Contract):
    use_case_id: str
    case_id: str | None = None
    title: str
    steps: tuple[StepProgress, ...]
    next_step_id: StageId | None = None
    model_readiness: ReadinessFact
    platform_readiness: tuple[ReadinessFact, ...] = ()
    limitations: tuple[str, ...] = ()


class PolicyReference(Contract):
    platform: str
    title: str
    url: HttpUrl
    retrieved_at: date | datetime | None = None
    version: str | None = None
    source_status: Literal["reference_only", "unverified"] = "reference_only"
    applicability: Literal["unknown"] = "unknown"

    @field_validator("retrieved_at")
    @classmethod
    def no_naive_policy_timestamp(cls, value):
        if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Policy retrieval timestamps must include a timezone.")
        return value


class DossierReadiness(Contract):
    status: Literal["ready_for_export", "needs_review", "unavailable"]
    summary: str
    case_id: str | None = None
    assessment_fingerprint: str | None = None
    included_finding_ids: tuple[str, ...] = ()
    unreviewed_finding_ids: tuple[str, ...] = ()
    withheld_finding_ids: tuple[str, ...] = ()
    missing_evidence_finding_ids: tuple[str, ...] = ()
    invalid_reference_ids: tuple[str, ...] = ()
    unavailable_record_ids: tuple[str, ...] = ()
    unknown_availability_record_ids: tuple[str, ...] = ()
    current_decision_count: int = 0
    policy_references: tuple[PolicyReference, ...] = ()
    policy_status: Literal["unknown"] = "unknown"
    legal_status: Literal["not_assessed"] = "not_assessed"
    submission_status: Literal["not_submitted"] = "not_submitted"
    limitations: tuple[str, ...] = ()
