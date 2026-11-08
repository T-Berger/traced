"""Validated application records; JSON dictionaries stop at explicit wire adapters.

EvidenceBundle owns immutable source contracts. These records describe mutable
review workflow state and never emulate dictionaries. A ReviewView holds the
loaded bundle directly instead of serializing and validating every source again.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from .contracts import Case, EvidenceBundle, Finding
from .verifier import Objection

ReviewMode = Literal["demo", "openrouter", "ollama", "swarm_recompute", "category_proposal"]
Disposition = Literal["use", "qualify", "withhold"]


class WorkflowRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CaseSummary(WorkflowRecord):
    id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    record_count: StrictInt = Field(ge=0)
    finding_count: StrictInt = Field(ge=0)
    run_count: StrictInt = Field(ge=0)
    created_at: str

    @model_validator(mode="after")
    def identity_alias_matches(self) -> CaseSummary:
        if self.id != self.case_id:
            raise ValueError("Case summary identity aliases disagree")
        return self


class CaseMetadata(CaseSummary):
    case: Case


class ScenarioState(WorkflowRecord):
    id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    base_run_id: str = Field(min_length=1)
    version: StrictInt = Field(ge=1)
    excluded_record_ids: tuple[str, ...] = ()
    reviewer: str = Field(min_length=1)
    created_at: str
    updated_at: str


class AssessmentState(BaseModel):
    """Named presentation of a writable local scenario or an immutable saved assessment."""

    model_config = ConfigDict(extra="allow", frozen=True)
    id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    base_run_id: str = Field(min_length=1)
    version: StrictInt = Field(ge=1)
    excluded_record_ids: tuple[str, ...] = ()
    label: str
    is_baseline: StrictBool = False
    assessment_kind: Literal["baseline", "scenario"] = "scenario"
    imported: StrictBool = False
    # None means a matching local request does not prove local computation.
    locally_computed: StrictBool | None = False


class ReviewDecision(WorkflowRecord):
    id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    finding_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    scenario_version: StrictInt = Field(ge=1)
    case_fingerprint: str | None = None
    reviewer: str = Field(min_length=1)
    disposition: Disposition
    reason: str = Field(min_length=1)
    created_at: str


class ScenarioCreated(WorkflowRecord):
    base_run_id: str = Field(min_length=1)
    scenario_version: StrictInt = Field(ge=1)
    assessment_kind: Literal["baseline", "scenario"] = "scenario"


class ExclusionChanged(WorkflowRecord):
    record_id: str = Field(min_length=1)
    excluded: StrictBool
    reason: str
    scenario_version: StrictInt = Field(ge=1)


class AuditEvent(WorkflowRecord):
    id: str
    case_id: str
    scenario_id: str | None
    event_type: Literal["scenario_created", "exclusion_changed", "finding_reviewed"]
    reviewer: str
    payload: ScenarioCreated | ExclusionChanged | ReviewDecision
    created_at: str

    @model_validator(mode="after")
    def payload_matches_event(self) -> AuditEvent:
        expected = {
            "scenario_created": ScenarioCreated,
            "exclusion_changed": ExclusionChanged,
            "finding_reviewed": ReviewDecision,
        }[self.event_type]
        if not isinstance(self.payload, expected):
            raise ValueError("Audit payload does not match its event type")
        return self

    @property
    def decision(self) -> ReviewDecision | None:
        return self.payload if isinstance(self.payload, ReviewDecision) else None


class Impact(WorkflowRecord):
    affected_finding_ids: tuple[str, ...] = ()
    remaining_evidence: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    excluded_record_ids: tuple[str, ...] = ()


class SourceGroup(WorkflowRecord):
    id: str
    kind: Literal["exact_text", "documented_repost", "exact_content"]
    label: str
    record_ids: tuple[str, ...]
    origin_record_id: str | None = None
    independence: str = "unknown"
    basis: str


class SearchScope(BaseModel):
    """Named provenance fields plus additive provider-specific audit metadata."""

    model_config = ConfigDict(extra="allow", frozen=True)
    fragment_ids: tuple[str, ...] = ()
    record_ids: tuple[str, ...] = ()
    excluded_record_ids: tuple[str, ...] = ()
    record_count: StrictInt = Field(default=0, ge=0)
    total_record_count: StrictInt | None = Field(default=None, ge=0)
    limitations: tuple[str, ...] = ()
    provider: str | None = None
    model: str | None = None


class FindingCorrespondence(WorkflowRecord):
    baseline_snapshot_ids: tuple[str, ...]
    result_snapshot_ids: tuple[str, ...]
    relation: Literal["removed", "continuation", "uncertain", "split", "merge", "added"]
    basis: str


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    findings: tuple[Objection, ...] = Field(default=(), max_length=8)
    search_scope: SearchScope | None = None
    mode: ReviewMode | None = None
    label: str | None = None
    queries: tuple[str, ...] = ()
    message: str | None = None
    stale: StrictBool = False
    cached: StrictBool | None = None
    kind: Literal["swarm_recompute", "category_proposal"] | None = None
    scenario_id: str | None = None
    scenario_version: StrictInt | None = Field(default=None, ge=1)
    computed_scenario_id: str | None = None
    result_run_id: str | None = None
    correspondences: tuple[FindingCorrespondence, ...] = ()


class CategoryRecord(WorkflowRecord):
    id: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)


class ReviewJobPayload(WorkflowRecord):
    case_id: str = Field(min_length=1)
    case_fingerprint: str = Field(min_length=1)
    finding_id: str = Field(min_length=1)
    finding_snapshot: Finding
    scenario_id: str = Field(min_length=1)
    scenario_version: StrictInt = Field(ge=1)
    excluded_record_ids: tuple[str, ...] = ()
    base_run_id: str = Field(min_length=1)
    mode: ReviewMode
    model: str | None = None
    model_identity: str | None = None
    prompt_version: str | None = None
    retrieval_version: str | None = None
    reason: str | None = None
    record_id: str | None = None
    record_content_hash: str | None = None
    record_manifest: tuple[CategoryRecord, ...] = ()
    target: str | None = None

    @model_validator(mode="after")
    def snapshot_matches_context(self) -> ReviewJobPayload:
        if self.finding_id != self.finding_snapshot.snapshot_id:
            raise ValueError("Queued finding ID does not match its snapshot")
        if self.base_run_id != self.finding_snapshot.run_id:
            raise ValueError("Queued baseline run does not match its finding snapshot")
        if self.mode == "category_proposal":
            if not all(
                isinstance(v, str) and v.strip()
                for v in (
                    self.record_id,
                    self.record_content_hash,
                    self.target,
                    self.model_identity,
                )
            ):
                raise ValueError(
                    "Category proposals require a saved text, target and model identity"
                )
            if len(self.target) > 500:
                raise ValueError("Category proposal target is too long")
            if self.record_id in self.excluded_record_ids:
                raise ValueError("The selected record is excluded from this assessment")
            ids = [item.id for item in self.record_manifest]
            if not 1 <= len(ids) <= 20 or len(ids) != len(set(ids)):
                raise ValueError("Select 1 to 20 distinct saved records")
            if (
                self.record_manifest[0].id != self.record_id
                or self.record_manifest[0].content_hash != self.record_content_hash
            ):
                raise ValueError("The selected record does not match the pinned manifest")
            if set(ids).intersection(self.excluded_record_ids):
                raise ValueError("An input record is excluded from this assessment")
        return self


class ReviewJob(WorkflowRecord):
    id: str = Field(min_length=1)
    case_id: str
    finding_id: str
    scenario_id: str
    mode: ReviewMode
    status: Literal["queued", "running", "completed", "failed"]
    payload: ReviewJobPayload
    result: ReviewResult | None = None
    error: str | None = None
    attempts: StrictInt = Field(ge=0)
    lease_expires_at: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    stale: StrictBool = False

    @model_validator(mode="after")
    def context_matches_payload(self) -> ReviewJob:
        for name in ("case_id", "finding_id", "scenario_id", "mode"):
            if getattr(self, name) != getattr(self.payload, name):
                raise ValueError(f"Queued {name} does not match its payload")
        return self

    @property
    def active(self) -> bool:
        return self.status in {"queued", "running"}


class ReviewSession(BaseModel):
    """Validated browser state, serialized only when crossing the Dash boundary."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    case_id: str | None = Field(default=None, min_length=1)
    case_fingerprint: str | None = Field(default=None, min_length=1)
    finding_id: str | None = Field(default=None, min_length=1)
    record_id: str | None = Field(default=None, min_length=1)
    scenario_id: str | None = Field(default=None, min_length=1)
    scenario_version: StrictInt | None = Field(default=None, ge=1)
    job_id: str | None = Field(default=None, min_length=1)
    comparison_scenario_id: str | None = Field(default=None, min_length=1)
    navigate_to: Literal["cases", "review", "compare", "brief"] | None = None
    draft_assessment: str | None = None
    draft_version: StrictInt | None = Field(default=None, ge=1)
    revision: StrictInt = Field(default=0, ge=0)

    def wire(self) -> dict[str, Any]:
        # Omit untouched optional draft/navigation fields so existing browser
        # fallbacks distinguish an absent label from an explicitly saved value.
        return self.model_dump(mode="json", exclude_unset=True)


@dataclass(frozen=True, slots=True)
class ReviewView:
    bundle: EvidenceBundle
    scenario: ScenarioState | AssessmentState | None
    impact: Impact
    groups: tuple[SourceGroup, ...]
    events: tuple[AuditEvent, ...]
    case_fingerprint: str
    # Filtering findings never clones the source text, links, or bundle contracts.
    findings: tuple[Finding, ...] | None = None
    computation: ReviewJob | None = None
    assessments: tuple[AssessmentState, ...] = ()
    baseline_scenario: AssessmentState | None = None
    is_baseline: bool = True

    @property
    def visible_findings(self) -> Sequence[Finding]:
        return self.findings if self.findings is not None else self.bundle.findings

    def wire(self) -> dict[str, Any]:
        data = self.bundle.model_dump(mode="json")
        if self.findings is not None:
            data["findings"] = [finding.model_dump(mode="json") for finding in self.findings]
        result = {
            "bundle": data,
            "scenario": self.scenario.model_dump(mode="json") if self.scenario else None,
            "impact": self.impact.model_dump(mode="json"),
            "groups": [group.model_dump(mode="json") for group in self.groups],
            "events": [event.model_dump(mode="json") for event in self.events],
            "case_fingerprint": self.case_fingerprint,
            "assessments": [
                item.model_dump(mode="json", exclude_unset=True) for item in self.assessments
            ],
            "baseline_scenario": (
                self.baseline_scenario.model_dump(mode="json", exclude_unset=True)
                if self.baseline_scenario
                else None
            ),
            "is_baseline": self.is_baseline,
            "assessment_kind": "baseline" if self.is_baseline else "scenario",
        }
        if self.computation is not None:
            result["computation"] = self.computation.model_dump(mode="json", exclude_unset=True)
        return result


@dataclass(frozen=True, slots=True)
class ReviewHistory:
    """The small context needed by a history panel; no source bundle is loaded."""

    events: tuple[AuditEvent, ...]
    scenario: ScenarioState | AssessmentState | None
    case_fingerprint: str
