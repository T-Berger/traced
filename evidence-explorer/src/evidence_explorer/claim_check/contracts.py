"""Validated boundaries for external evidence collection and proposed assessments."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..review.contracts import Finding, Fragment, Record
from ..review.origins import claim_synthetic_origin


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Issue(Contract):
    code: str
    message: str
    url: str | None = None
    query: str | None = None


class SearchHit(Contract):
    url: str
    title: str
    snippet: str = ""
    rank: int = Field(default=1, ge=1)
    query: str = ""
    provider: str = ""


class SearchBatch(Contract):
    query: str
    provider: str
    searched_at: str
    hits: list[SearchHit] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class SavedEvidence(Contract):
    id: str
    url: str
    requested_url: str
    title: str
    publisher: str
    text: str = Field(min_length=1)
    published_at: str | None = None
    retrieved_at: str
    content_hash: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_hash(self) -> SavedEvidence:
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.content_hash:
            raise ValueError("Saved evidence content hash does not match its text.")
        return self


class EvidenceCollection(Contract):
    records: list[SavedEvidence] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    attempted_urls: list[str] = Field(default_factory=list)


class CheckPolicy(Contract):
    queries: list[str] = Field(default_factory=list, max_length=3)
    seed_urls: list[str] = Field(default_factory=list, max_length=6)
    max_sources: int = Field(default=6, ge=1, le=8)
    results_per_query: int = Field(default=5, ge=1, le=8)

    @model_validator(mode="after")
    def nonempty_values(self) -> CheckPolicy:
        if any(not value.strip() or len(value) > 1000 for value in self.queries):
            raise ValueError("Queries must contain 1–1,000 characters.")
        if any(not value.strip() or len(value) > 4096 for value in self.seed_urls):
            raise ValueError("Source URLs must contain 1–4,096 characters.")
        return self


class EvidenceFragment(Contract):
    id: str
    record_id: str
    text: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class EvidenceSnapshot(Contract):
    records: list[SavedEvidence] = Field(default_factory=list)
    fragments: list[EvidenceFragment] = Field(default_factory=list)
    searches: list[SearchBatch] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    attempted_urls: list[str] = Field(default_factory=list)
    created_at: str
    retrieval_version: str = "external-evidence-v1"
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_fragments(self) -> EvidenceSnapshot:
        records = {record.id: record for record in self.records}
        if len(records) != len(self.records):
            raise ValueError("Duplicate evidence record IDs.")
        if len({fragment.id for fragment in self.fragments}) != len(self.fragments):
            raise ValueError("Duplicate evidence fragment IDs.")
        for fragment in self.fragments:
            record = records.get(fragment.record_id)
            if (
                record is None
                or not 0 <= fragment.start < fragment.end <= len(record.text)
                or record.text[fragment.start : fragment.end] != fragment.text
            ):
                raise ValueError("Evidence fragment does not match its saved source.")
        return self

    def fingerprint(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AssessmentCitation(Contract):
    fragment_id: str
    quote: str = Field(min_length=1)
    stance: Literal["supports", "contradicts", "context"]


class ProposedAssessment(Contract):
    status: Literal[
        "supported", "contradicted", "misleading_context", "insufficient_evidence", "not_factual"
    ]
    explanation: str = Field(min_length=1)
    citations: list[AssessmentCitation] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    model: str | None = None
    provider: str | None = None
    prompt_version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def declared_finding_origin(finding: Finding) -> bool | None:
    """Return only an explicit finding flag; case/record origin cannot supply it."""
    return claim_synthetic_origin(finding.model_dump(mode="json"))


class ClaimCheckPayload(Contract):
    source_case_id: str
    source_finding_id: str
    source_case_title: str
    source_case_fingerprint: str
    # New jobs record selected-finding and surrounding-case origin separately.
    # Older saved jobs used source_synthetic for the case flag; their pinned
    # finding is authoritative when a result is emitted or retried.
    source_synthetic: bool | None = None
    source_case_synthetic: bool | None = None
    origin_records: list[Record] = Field(default_factory=list, max_length=8)
    origin_fragments: list[Fragment] = Field(default_factory=list, max_length=64)
    origin_limitations: list[str] = Field(default_factory=list)
    finding_snapshot: Finding
    policy: CheckPolicy
    requested_model: str
    retry_of: str | None = None

    @model_validator(mode="before")
    @classmethod
    def retain_legacy_case_origin(cls, value: Any) -> Any:
        # Only an absent field denotes the legacy envelope. An explicit null
        # is an unknown case origin and must remain unknown after serialization.
        if isinstance(value, dict) and "source_case_synthetic" not in value:
            return {**value, "source_case_synthetic": value.get("source_synthetic")}
        return value

    @property
    def claim(self) -> str:
        return self.finding_snapshot.statement

    @property
    def claim_synthetic(self) -> bool | None:
        return declared_finding_origin(self.finding_snapshot)

    @property
    def case_synthetic(self) -> bool | None:
        return self.source_case_synthetic


class ClaimCheckResult(Contract):
    result_case_id: str
    result_finding_id: str
    assessment: ProposedAssessment
    evidence_fingerprint: str
    coverage: Literal["collected", "partial", "empty"]
    sources_count: int = Field(ge=0)
    issues: list[Issue] = Field(default_factory=list)


class ClaimCheckJob(Contract):
    id: str
    source_case_id: str
    source_finding_id: str
    status: Literal["queued", "running", "completed", "failed", "stale"]
    stage: str
    payload: ClaimCheckPayload
    snapshot: EvidenceSnapshot | None = None
    result: ClaimCheckResult | None = None
    error: str | None = None
    attempts: int = Field(default=0, ge=0)
    lease_expires_at: str | None = None
    created_at: str
    updated_at: str
    finished_at: str | None = None
    stale: bool = False

    @model_validator(mode="after")
    def validate_context(self) -> ClaimCheckJob:
        if (
            self.source_case_id != self.payload.source_case_id
            or self.source_finding_id != self.payload.source_finding_id
            or self.source_finding_id != self.payload.finding_snapshot.snapshot_id
        ):
            raise ValueError("Claim-check job source context does not match its payload.")
        return self
