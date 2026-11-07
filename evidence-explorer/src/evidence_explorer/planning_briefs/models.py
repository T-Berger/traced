"""Immutable planning records; a saved confirmation never grants another revision approval."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..use_cases.contracts import AlertRule, WindowDefinition

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
MAX_REVISION_BYTES = 1024 * 1024
MAX_HISTORY_BYTES = 20 * 1024 * 1024
BriefWindow = WindowDefinition
BriefAlertRule = AlertRule


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class ImmutableModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    @field_validator("*", mode="after")
    @classmethod
    def aware_dates(cls, value):
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Planning timestamps require an explicit timezone.")
            return value.astimezone(UTC)
        return value


class DraftFields(ImmutableModel):
    """Partial analyst input, deliberately unable to assert confirmation."""

    use_case_id: str | None = Field(default=None, min_length=1, max_length=100)
    purpose: str | None = Field(default=None, max_length=4000)
    public_subjects_or_topics: tuple[str, ...] = Field(default=(), max_length=100)
    source_selection: tuple[str, ...] = Field(default=(), max_length=2000)
    reviewer_role: str | None = Field(default=None, max_length=500)
    expected_output: Literal["reviewed_brief", "evidence_dossier"] | None = None
    baseline_window: WindowDefinition | None = None
    observation_window: WindowDefinition | None = None
    alert_rule: AlertRule | None = None
    information_scope: Literal["public_information"] = "public_information"
    languages: tuple[str, ...] = Field(default=(), max_length=100)

    @field_validator("public_subjects_or_topics", "source_selection", "languages")
    @classmethod
    def unique_visible_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            not value.strip() or len(value) > 2000 for value in values
        ):
            raise ValueError("Planning values must be unique, nonblank, and bounded.")
        return values

    @model_validator(mode="after")
    def alert_windows_are_compatible(self):
        if self.alert_rule and self.alert_rule.comparison_basis == "equal_duration_counts":
            windows = (self.baseline_window, self.observation_window)
            if all(
                window and window.start is not None and window.end is not None for window in windows
            ):
                durations = [
                    window.end.astimezone(UTC) - window.start.astimezone(UTC) for window in windows
                ]
                if durations[0] != durations[1]:
                    raise ValueError("Equal-count alert rules require equal UTC window durations.")
        return self


class SavedRecordRef(ImmutableModel):
    record_id: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    source_label: str
    source_url: str | None = None
    metadata_json: str

    @model_validator(mode="after")
    def metadata_agrees(self):
        try:
            metadata = json.loads(self.metadata_json)
        except (ValueError, TypeError):
            raise ValueError("Saved record metadata must be a JSON object.") from None
        if (
            not isinstance(metadata, dict)
            or any(
                metadata.get(key) != value
                for key, value in (
                    ("id", self.record_id),
                    ("content_hash", self.content_hash),
                    ("source_label", self.source_label),
                    ("source_url", self.source_url),
                )
            )
            or "text" in metadata
        ):
            raise ValueError("Saved record metadata disagrees with its declared source reference.")
        return self


class SourceObservation(ImmutableModel):
    source_id: str = Field(min_length=1)
    item_key: str = Field(min_length=1)
    payload_hash: Digest | None = None
    semantic_hash: Digest | None = None
    content_hash: Digest | None = None
    first_observed_at: datetime | None = None
    observed_at: datetime | None = None
    attribution: Literal["observed", "unknown"]

    @model_validator(mode="after")
    def observed_details_are_complete(self):
        hashes = (self.payload_hash, self.semantic_hash, self.content_hash)
        if self.attribution == "observed":
            if any(value is None for value in (*hashes, self.first_observed_at, self.observed_at)):
                raise ValueError("Observed provenance needs exact hashes and capture times.")
            if self.first_observed_at > self.observed_at:
                raise ValueError("An observation cannot precede its first capture.")
        elif any(value is not None for value in hashes):
            raise ValueError("Unknown provenance cannot assert observed hashes.")
        return self


class ScopeIdentity(ImmutableModel):
    policy: Literal["saved-evidence-and-scope/v1"] = "saved-evidence-and-scope/v1"
    case_id: str = Field(min_length=1)
    evidence_fingerprint: Digest
    scope_fingerprint: Digest
    basis: Literal["collection_scope", "saved_evidence_manifest"]
    source_refs: tuple[str, ...]
    records: tuple[SavedRecordRef, ...]
    collection_scope_fingerprint: Digest | None = None
    observations_fingerprint: Digest | None = None
    source_observations: tuple[SourceObservation, ...] = ()
    collection_scope_json: str | None = None

    @model_validator(mode="after")
    def intact_scope(self):
        payload = self.model_dump(mode="json", exclude={"scope_fingerprint"})
        if self.scope_fingerprint != digest(payload):
            raise ValueError("Saved scope identity hash mismatch.")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("Saved scope source references must be unique.")
        if self.basis == "saved_evidence_manifest":
            if any(
                (
                    self.collection_scope_fingerprint,
                    self.observations_fingerprint,
                    self.source_observations,
                    self.collection_scope_json,
                )
            ):
                raise ValueError("A supplied record manifest cannot assert collection provenance.")
            if set(self.source_refs) != {record.record_id for record in self.records}:
                raise ValueError("A supplied scope must include the whole saved record manifest.")
        elif not all(
            (
                self.collection_scope_fingerprint,
                self.observations_fingerprint,
                self.collection_scope_json,
            )
        ):
            raise ValueError("A collection scope requires its exact saved provenance.")
        else:
            try:
                scope = json.loads(self.collection_scope_json)
                observations = tuple(
                    SourceObservation.model_validate(row) for row in scope["observation_manifest"]
                )
                refs = scope["source_ids"]
                source_ids = [row["id"] for row in scope["sources"]]
                keys = [(row.source_id, row.item_key) for row in observations]
                valid = (
                    sorted(refs) == sorted(self.source_refs) == sorted(source_ids)
                    and len(refs) == len(set(refs))
                    and len(keys) == len(set(keys))
                    and all(row.source_id in refs for row in observations)
                    and observations == self.source_observations
                    and scope["fingerprint"] == self.collection_scope_fingerprint
                    and scope["observations_fingerprint"] == self.observations_fingerprint
                )
            except (ValueError, TypeError, KeyError):
                valid = False
            if not valid:
                raise ValueError(
                    "Collection identity disagrees with its exact saved source manifest."
                )
        return self


class BriefBinding(ImmutableModel):
    case_id: str = Field(min_length=1)
    source_case_fingerprint: Digest
    scope: ScopeIdentity

    @model_validator(mode="after")
    def same_case(self):
        if self.case_id != self.scope.case_id:
            raise ValueError("A binding and its saved scope must describe the same case.")
        return self


def revision_content_hash(fields: DraftFields, binding: BriefBinding | None) -> str:
    return digest(
        {
            "fields": fields.model_dump(mode="json"),
            "binding": binding.model_dump(mode="json") if binding else None,
        }
    )


class BriefRevision(ImmutableModel):
    brief_id: str = Field(min_length=1, max_length=100)
    revision_id: str = Field(min_length=1, max_length=100)
    number: int = Field(ge=1)
    parent_revision_id: str | None = Field(default=None, min_length=1, max_length=100)
    fields: DraftFields
    binding: BriefBinding | None = None
    content_hash: Digest
    author: str = Field(min_length=1, max_length=500)
    created_at: datetime

    @model_validator(mode="after")
    def intact_content(self):
        if not self.author.strip():
            raise ValueError("A revision author must be supplied.")
        if self.content_hash != revision_content_hash(self.fields, self.binding):
            raise ValueError("Planning revision content hash mismatch.")
        if (self.number == 1) != (self.parent_revision_id is None):
            raise ValueError("Only the first revision has no parent.")
        if len(canonical_json(self.model_dump(mode="json")).encode("utf-8")) > MAX_REVISION_BYTES:
            raise ValueError("Planning revision exceeds the 1 MiB saved-scope limit.")
        return self


class BriefConfirmation(ImmutableModel):
    confirmation_id: str = Field(min_length=1, max_length=100)
    brief_id: str = Field(min_length=1, max_length=100)
    revision_id: str = Field(min_length=1, max_length=100)
    binding_digest: Digest
    reviewer: str = Field(min_length=1, max_length=500)
    confirmed_at: datetime
    provenance: Literal["local", "imported"] = "local"

    @field_validator("reviewer")
    @classmethod
    def visible_reviewer(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A confirming reviewer must be supplied.")
        return value


class BriefState(ImmutableModel):
    revision: BriefRevision
    confirmation: BriefConfirmation | None = None
    status: Literal["draft", "unbound", "needs_review", "confirmed"]
    reasons: tuple[str, ...] = ()
    head_revision_id: str | None = None
    is_head: bool = False
    imported: bool = False
    confirmation_is_local: bool = False
    is_active_for_case: bool = False


class PlanningHistory(ImmutableModel):
    schema_version: Literal[1] = 1
    revisions: tuple[BriefRevision, ...] = Field(default=(), max_length=1000)
    confirmations: tuple[BriefConfirmation, ...] = Field(default=(), max_length=1000)

    @model_validator(mode="after")
    def unique_identities(self):
        for values, attribute in (
            (self.revisions, "revision_id"),
            (self.confirmations, "confirmation_id"),
        ):
            ids = [getattr(value, attribute) for value in values]
            if len(ids) != len(set(ids)):
                raise ValueError("Planning history contains duplicate identities.")
        return self
