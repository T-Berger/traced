"""Evaluate explicitly declared activity in a complete saved evidence snapshot.

This pure adapter counts saved record identities, never classifies their content,
and grants no collection, model, or review authority. Coverage is an explicit
saved bounded-sample assertion, not proof of whole-platform completeness.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..planning_briefs.models import BriefState, ScopeIdentity, digest
from ..planning_briefs.progress import confirmed_progress_facts, scope_context
from ..planning_briefs.scope import scope_identity
from ..planning_briefs.service import confirmation_issues
from ..review.contracts import EvidenceBundle, Record
from ..review.workflow import evidence_fingerprint
from ..source_contracts import SourceItem
from .alerts import evaluate_activity_alert
from .contracts import ActivityWindow, AlertEvaluation, AlertRule, WindowDefinition

TimestampBasis = Literal["publication", "acquisition", "event"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class SavedActivityRequest(_Frozen):
    """An explicit saved-rule request; windows are never supplied by the caller."""

    rule: AlertRule
    timestamp_basis: TimestampBasis
    record_kind: str = Field(min_length=1, max_length=200)

    @field_validator("record_kind")
    @classmethod
    def visible_kind(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Choose an explicit saved record kind.")
        return value


class SavedActivityCoverageWindow(_Frozen):
    start: datetime
    end: datetime
    coverage_status: Literal["complete", "partial", "unknown"]
    coverage_fraction: float | None = Field(ge=0, le=1, allow_inf_nan=False)
    evidence: str = Field(min_length=1, max_length=4000)
    exposure: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    exposure_unit: str | None = Field(default=None, max_length=200)

    @field_validator("coverage_fraction", "exposure", mode="before")
    @classmethod
    def numbers_are_not_flags(cls, value):
        if isinstance(value, bool):
            raise ValueError("Coverage and exposure must be numeric measurements.")
        return value

    @field_validator("start", "end")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Coverage windows require explicit timezones.")
        return value.astimezone(UTC)

    @field_validator("evidence", "exposure_unit")
    @classmethod
    def visible_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Coverage evidence and exposure units cannot be blank.")
        return value

    @model_validator(mode="after")
    def coherent_window(self):
        if self.start >= self.end:
            raise ValueError("Coverage windows must end after they start.")
        if self.coverage_status == "unknown" and self.coverage_fraction is not None:
            raise ValueError("Unknown coverage cannot assert a fraction.")
        return self


class SavedActivityCoverage(_Frozen):
    """Saved assertion for these exact sources, record kind, time basis and windows."""

    policy: Literal["saved-activity-coverage/v1"]
    count_unit: Literal["saved_record_snapshots"] = "saved_record_snapshots"
    timestamp_basis: TimestampBasis
    record_kind: str = Field(min_length=1, max_length=200)
    source_refs: tuple[str, ...] = Field(min_length=1)
    baseline: SavedActivityCoverageWindow
    observation: SavedActivityCoverageWindow

    @field_validator("source_refs")
    @classmethod
    def exact_unique_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not value.strip() for value in values):
            raise ValueError("Coverage source references must be unique and nonblank.")
        return tuple(sorted(values))


class SavedActivityResult(_Frozen):
    status: Literal["review_needed", "not_triggered", "unavailable"]
    summary: str
    case_id: str | None
    case_fingerprint: str | None
    evidence_fingerprint: str | None
    scope_fingerprint: str | None
    scope_basis: Literal["collection_scope", "saved_evidence_manifest"] | None
    source_refs: tuple[str, ...] = ()
    count_unit: Literal["saved_record_snapshots"] = "saved_record_snapshots"
    brief_id: str | None = None
    revision_id: str | None = None
    revision_fingerprint: str | None = None
    binding_fingerprint: str | None = None
    request_fingerprint: str
    context_fingerprint: str
    baseline_count: int | None = None
    observation_count: int | None = None
    baseline_record_ids: tuple[str, ...] = ()
    observation_record_ids: tuple[str, ...] = ()
    outside_window_record_ids: tuple[str, ...] = ()
    omitted_record_ids: tuple[str, ...] = ()
    unknown_time_record_ids: tuple[str, ...] = ()
    unpackaged_source_observation_count: int = Field(
        default=0, description="Differing saved versions omitted; excludes item-limit records."
    )
    item_limit_omitted_record_count: int | None = None
    source_gaps: tuple[str, ...] = ()
    source_disclosures: tuple[str, ...] = ()
    baseline: ActivityWindow | None = None
    observation: ActivityWindow | None = None
    coverage: SavedActivityCoverage | None = None
    evaluation: AlertEvaluation | None = None
    method: str
    limitations: tuple[str, ...]


_LIMITS = (
    "Counts describe saved record IDs of the explicitly selected kind; no content is classified.",
    "Separate saved snapshots are not deduplicated into native posts or independent corroboration.",
    "Saved coverage is a bounded-sample assertion, not verified whole-platform coverage.",
    "No publication, acquisition, event time, source membership, or missing record is inferred.",
    "A signal requests human review; it establishes no truth, hate, automation, or intent.",
    "This read-only evaluation creates no live alert, job, review decision, or platform report.",
)


def _record_time(record: Record, basis: TimestampBasis) -> datetime | None:
    raw = (
        record.published_at
        if basis == "publication"
        else record.acquired_at
        if basis == "acquisition"
        else record.metadata.get("event_time")
    )
    return _explicit_time(raw)


def _explicit_time(raw) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        moment = datetime.fromisoformat(raw)
        if moment.tzinfo is None or moment.utcoffset() is None:
            return None
        return moment.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        return None


def _native_key(record: Record) -> str | None:
    identity = tuple(record.metadata.get(key) for key in ("platform", "channel_id", "native_id"))
    return (
        digest(list(identity)) if all(isinstance(part, str) and part for part in identity) else None
    )


def _source_payload_hash(record: Record) -> str | None:
    """Recover only the producer's explicit saved SourceItem fields, with no fallback."""
    metadata = record.metadata
    payload = {
        name: metadata.get(name)
        for name in (
            "platform",
            "channel_id",
            "native_id",
            "channel_name",
            "author_id",
            "title",
            "shared_urls",
            "repost_of",
            "provenance",
        )
    }
    payload.update(
        text=record.text,
        source_url=record.source_url,
        published_at=record.published_at,
        acquired_at=record.acquired_at,
    )
    try:
        return digest(SourceItem.model_validate(payload).model_dump(mode="json"))
    except (ValueError, TypeError):
        return None


def _included_record_gaps(
    scope: ScopeIdentity, records: tuple[Record, ...], record_kind: str
) -> tuple[str, ...]:
    """Reconcile the complete native union, not sums of source memberships.

    The producer chooses one version per native identity, including records
    outside the activity windows. Identical selected-source observations share
    that record; differing payloads need explicit omission witnesses, and none
    of those witnesses can replace the chosen record itself.
    """
    saved = json.loads(scope.collection_scope_json)
    observed: dict[str, list] = {}
    for row in scope.source_observations:
        if row.attribution == "observed":
            observed.setdefault(row.item_key, []).append(row)
    winners = {
        key: max(sorted(rows, key=lambda row: row.source_id), key=lambda row: row.observed_at)
        for key, rows in observed.items()
    }
    actual: dict[str, list[Record]] = {}
    for record in records:
        key = _native_key(record)
        if key is not None:
            actual.setdefault(key, []).append(record)
    gaps = []
    if saved.get("total_items") != len(winners):
        gaps.append("Declared total native records disagree with the saved observation union.")
    if saved.get("record_count") != len(winners) or len(actual) != len(winners):
        gaps.append("Declared included native records do not match the complete saved bundle.")
    for key, winner in sorted(winners.items()):
        matching = actual.get(key, [])
        differing = len({row.payload_hash for row in observed[key]}) > 1
        if (
            differing
            and saved.get("version_policy") != "latest_selected_observation_then_source_id"
        ):
            gaps.append(f"{key}: conflicting saved payloads have no supported version policy.")
        if len(matching) != 1:
            gaps.append(f"{key}: the declared included native record is missing or repeated.")
        elif matching[0].kind != record_kind or (
            (
                matching[0].metadata.get("domain") == "sources"
                or matching[0].id.startswith("sources:record:")
            )
            and matching[0].kind != "saved_source_item"
        ):
            gaps.append(
                f"{key}: the included native record kind differs from the declared projection."
            )
        elif matching[0].content_hash != winner.content_hash or matching[0].text is None:
            gaps.append(f"{key}: the chosen saved native content is missing or different.")
        elif (matching[0].kind == "saved_source_item" or differing) and _source_payload_hash(
            matching[0]
        ) != winner.payload_hash:
            gaps.append(f"{key}: the exact chosen native payload is missing, different or unknown.")
    if set(actual) - set(winners):
        gaps.append(
            "The complete bundle contains native records outside its declared source union."
        )
    by_source: dict[str, list] = {}
    for rows in observed.values():
        for row in rows:
            by_source.setdefault(row.source_id, []).append(row)
    for source in saved["sources"]:
        source_id = source["id"]
        rows = by_source.get(source_id, [])
        included = [row for row in rows if row.payload_hash == winners[row.item_key].payload_hash]
        omitted = {
            row.item_key: row
            for row in rows
            if row.payload_hash != winners[row.item_key].payload_hash
        }
        if type(source.get("included_count")) is not int or source["included_count"] != len(
            included
        ):
            gaps.append(f"{source_id}: declared included memberships do not match chosen versions.")
        declarations = source.get("version_omissions", [])
        if (
            not isinstance(declarations, list)
            or any(not isinstance(row, dict) for row in declarations)
            or source.get("omitted_version_count") != len(omitted)
            or len(declarations) != len(omitted)
        ):
            gaps.append(f"{source_id}: differing versions lack exact saved omission declarations.")
            continue
        witnessed = set()
        for declaration in declarations:
            key = declaration.get("item_key")
            loser = omitted.get(key) if isinstance(key, str) else None
            winner = winners.get(key) if isinstance(key, str) else None
            valid = loser is not None and winner is not None and key not in witnessed
            if valid:
                expected = {
                    "payload_hash": loser.payload_hash,
                    "content_hash": loser.content_hash,
                    "included_source_id": winner.source_id,
                    "included_payload_hash": winner.payload_hash,
                    "included_content_hash": winner.content_hash,
                }
                valid = (
                    all(declaration.get(name) == value for name, value in expected.items())
                    and declaration.get("included_in_case") is True
                    and _explicit_time(declaration.get("observed_at")) == loser.observed_at
                    and _explicit_time(declaration.get("included_observed_at"))
                    == winner.observed_at
                    and type(declaration.get("observation_time_tied")) is bool
                    and declaration["observation_time_tied"]
                    == (loser.observed_at == winner.observed_at)
                )
                matched = actual.get(key, [])
                if len(matched) == 1:
                    valid = valid and all(
                        declaration.get(name) == matched[0].metadata.get(name)
                        for name in ("channel_id", "native_id")
                    )
            if not valid:
                gaps.append(f"{source_id}: a differing-version omission does not resolve exactly.")
            else:
                witnessed.add(key)
        if witnessed != set(omitted):
            gaps.append(
                f"{source_id}: not every differing observation has a valid omission witness."
            )
    return tuple(gaps)


def _source_gaps(
    scope: ScopeIdentity,
    candidates: tuple[Record, ...],
    records: tuple[Record, ...],
    record_kind: str,
) -> tuple[str, ...]:
    if scope.basis != "collection_scope":
        return ()
    saved = json.loads(scope.collection_scope_json)
    gaps = []
    by_source: dict[str, list] = {}
    known_content = set()
    for row in scope.source_observations:
        by_source.setdefault(row.source_id, []).append(row)
        if row.attribution == "observed":
            known_content.add((row.item_key, row.content_hash))
    total, included = saved.get("total_items"), saved.get("record_count")
    if (
        type(total) is not int
        or type(included) is not int
        or not 0 <= included <= total
        or type(saved.get("truncated")) is not bool
    ):
        gaps.append("The frozen selected-source record limit and omitted coverage are unknown.")
    elif saved["truncated"] or total > included:
        gaps.append("The frozen selected-source scope omits saved records at the case item limit.")
    for source in saved["sources"]:
        source_id = source["id"]
        if source.get("last_status") != "succeeded":
            gaps.append(f"{source_id}: frozen collection status is not succeeded.")
        observations = by_source.get(source_id, [])
        if any(row.attribution != "observed" for row in observations):
            gaps.append(f"{source_id}: saved observations have unknown attribution.")
        item_count = source.get("item_count")
        if type(item_count) is not int or item_count != len(observations):
            gaps.append(f"{source_id}: saved source observation coverage is incomplete or unknown.")
        elif item_count == 0:
            gaps.append(f"{source_id}: no saved observations establish per-window source coverage.")
        if source.get("unattributed_count", 0):
            gaps.append(f"{source_id}: unattributed source associations remain.")
        omitted_count = source.get("omitted_version_count", 0)
        if type(omitted_count) is not int or omitted_count < 0:
            gaps.append(f"{source_id}: omitted observation count is invalid or unknown.")
    for record in candidates:
        key = _native_key(record)
        if key is None or (key, record.content_hash) not in known_content:
            gaps.append(
                f"{record.id}: selected-source attribution for this saved content is unknown."
            )
    return (*gaps, *_included_record_gaps(scope, records, record_kind))


def _same_bounds(window: WindowDefinition, coverage: SavedActivityCoverageWindow) -> bool:
    return (
        window.start is not None
        and window.end is not None
        and window.start.astimezone(UTC) == coverage.start
        and window.end.astimezone(UTC) == coverage.end
    )


def evaluate_saved_activity(
    bundle: EvidenceBundle,
    state: BriefState | None,
    request: SavedActivityRequest,
) -> SavedActivityResult:
    """Evaluate a complete server-supplied snapshot and local confirmed active plan.

    Malformed requests are contract errors. Missing or invalid saved coverage,
    case/plan provenance and record times produce an honest unavailable result.
    The caller supplies complete evidence and trusted server state, never a
    display-filtered bundle or browser-authored confirmation.
    """
    request = SavedActivityRequest.model_validate(request)
    method = (
        f"Count unique saved record IDs with kind exactly {request.record_kind!r}; "
        f"use only the explicit {request.timestamp_basis} time field. "
        "Assign normalized UTC times to half-open [start, end) saved brief windows. "
        "Relative change = 100 × (observation value − baseline value) / baseline value; "
        "the configured threshold is inclusive."
    )
    values = {
        "case_id": None,
        "case_fingerprint": None,
        "evidence_fingerprint": None,
        "scope_fingerprint": None,
        "scope_basis": None,
        "request_fingerprint": digest(request.model_dump(mode="json")),
        "method": method,
        "limitations": _LIMITS,
    }
    if request.timestamp_basis == "event":
        values["limitations"] += (
            "metadata.event_time is a source-declared event assertion, "
            "not independently verified capture time.",
        )

    def result(reason: str, evaluation: AlertEvaluation | None = None) -> SavedActivityResult:
        context = {
            key: values.get(key)
            for key in (
                "case_id",
                "evidence_fingerprint",
                "scope_fingerprint",
                "revision_fingerprint",
                "binding_fingerprint",
                "request_fingerprint",
            )
        }
        return SavedActivityResult(
            **values,
            status=evaluation.status if evaluation else "unavailable",
            summary=evaluation.summary if evaluation else reason,
            evaluation=evaluation,
            context_fingerprint=digest(context),
        )

    try:
        bundle = EvidenceBundle.model_validate(bundle)
        values.update(
            case_id=bundle.case.id,
            case_fingerprint=bundle.fingerprint(),
            evidence_fingerprint=evidence_fingerprint(bundle.model_dump(mode="json")),
        )
        scope = scope_identity(bundle)
        values.update(
            scope_fingerprint=scope.scope_fingerprint,
            scope_basis=scope.basis,
            source_refs=scope.source_refs,
        )
        state = BriefState.model_validate(state) if state is not None else None
    except (ValueError, TypeError, KeyError):
        return result("Saved evidence or planning provenance is invalid or unsupported.")
    if state is not None:
        values.update(
            brief_id=state.revision.brief_id,
            revision_id=state.revision.revision_id,
            revision_fingerprint=digest(state.revision.model_dump(mode="json")),
            binding_fingerprint=(
                digest(state.revision.binding.model_dump(mode="json"))
                if state.revision.binding
                else None
            ),
        )
        if confirmation_issues(state.revision):
            return result("The saved planning revision is incomplete or invalid for confirmation.")
    facts = confirmed_progress_facts(state, scope_context(scope))
    plan = facts.brief
    if plan is None or facts.brief_binding is None:
        return result("A current locally confirmed active brief must match this saved scope.")
    if set(plan.source_selection) != set(scope.source_refs):
        return result("The confirmed brief does not select this exact saved source scope.")
    if plan.alert_rule is None or plan.alert_rule != request.rule:
        return result("The requested rule must equal the confirmed saved brief's explicit rule.")
    windows = (plan.baseline_window, plan.observation_window)
    if not all(
        window and window.start is not None and window.end is not None for window in windows
    ):
        return result("Complete baseline and observation windows must be saved and confirmed.")
    baseline_window, observation_window = windows
    if observation_window.start.astimezone(UTC) < baseline_window.end.astimezone(UTC):
        return result("Saved observation and baseline windows overlap or are out of order.")

    baseline_ids, observation_ids, outside, omitted, unknown = [], [], [], [], []
    by_id = {record.id: record for record in bundle.records}
    for record in sorted(bundle.records, key=lambda item: item.id):
        if record.kind != request.record_kind:
            omitted.append(record.id)
            continue
        moment = _record_time(record, request.timestamp_basis)
        if moment is None:
            unknown.append(record.id)
        elif baseline_window.start.astimezone(UTC) <= moment < baseline_window.end.astimezone(UTC):
            baseline_ids.append(record.id)
        elif (
            observation_window.start.astimezone(UTC)
            <= moment
            < observation_window.end.astimezone(UTC)
        ):
            observation_ids.append(record.id)
        else:
            outside.append(record.id)
    values.update(
        baseline_record_ids=tuple(baseline_ids),
        observation_record_ids=tuple(observation_ids),
        outside_window_record_ids=tuple(outside),
        omitted_record_ids=tuple(omitted),
        unknown_time_record_ids=tuple(unknown),
        baseline_count=None if unknown else len(baseline_ids),
        observation_count=None if unknown else len(observation_ids),
        source_gaps=_source_gaps(
            scope,
            tuple(record for record in bundle.records if record.kind == request.record_kind),
            tuple(bundle.records),
            request.record_kind,
        ),
    )
    if scope.basis == "collection_scope":
        saved_scope = json.loads(scope.collection_scope_json)
        values["unpackaged_source_observation_count"] = sum(
            count
            for source in saved_scope["sources"]
            if type(count := source.get("omitted_version_count", 0)) is int and count >= 0
        )
        total, included = saved_scope.get("total_items"), saved_scope.get("record_count")
        values["item_limit_omitted_record_count"] = (
            total - included
            if type(total) is int and type(included) is int and 0 <= included <= total
            else None
        )
        values["source_disclosures"] = tuple(
            f"{source['id']}: frozen connection readiness is "
            + ("unavailable" if source.get("ready") is False else "unknown")
            + "; it does not establish historical window coverage."
            for source in saved_scope["sources"]
            if source.get("ready") is not True
        )
        values["limitations"] += values["source_disclosures"]
        values["limitations"] += (
            "Only included saved record versions are counted; omitted source observations "
            "are not readable evidence and receive no invented record IDs.",
        )
    try:
        coverage = SavedActivityCoverage.model_validate(
            bundle.case.coverage.get("saved_activity_coverage")
        )
    except (ValueError, TypeError):
        return result("This saved case has no usable coverage declaration for these windows.")
    values["coverage"] = coverage
    if (
        coverage.timestamp_basis != request.timestamp_basis
        or coverage.record_kind != request.record_kind
        or set(coverage.source_refs) != set(scope.source_refs)
        or not _same_bounds(baseline_window, coverage.baseline)
        or not _same_bounds(observation_window, coverage.observation)
    ):
        return result(
            "Saved coverage does not match the exact kind, time basis, sources and windows."
        )
    if any(
        window.coverage_status != "complete" or window.coverage_fraction != 1
        for window in (coverage.baseline, coverage.observation)
    ):
        return result("Both saved windows require an explicit complete coverage assertion of one.")
    if values["source_gaps"]:
        return result("Frozen selected-source gaps prevent this bounded activity comparison.")
    if unknown:
        return result("Candidate saved records have missing, malformed or timezone-naive times.")

    def activity(window, declared, record_ids):
        return ActivityWindow(
            id=window.id,
            start=window.start,
            end=window.end,
            metric=request.rule.metric,
            source_ids=scope.source_refs,
            observed_count=len(record_ids),
            record_ids=tuple(record_ids),
            timestamp_basis=request.timestamp_basis,
            coverage_status="complete",
            coverage_fraction=1,
            collection_method="Saved record ID count with explicit bounded-sample coverage/v1",
            exposure=declared.exposure,
            exposure_unit=declared.exposure_unit,
            synthetic=bool(record_ids)
            and all(by_id[rid].metadata.get("synthetic") is True for rid in record_ids),
        )

    baseline = activity(baseline_window, coverage.baseline, baseline_ids)
    observation = activity(observation_window, coverage.observation, observation_ids)
    values.update(baseline=baseline, observation=observation)
    evaluation = evaluate_activity_alert(request.rule, baseline, observation)
    origins = {
        by_id[rid].metadata.get("synthetic") is True for rid in (*baseline_ids, *observation_ids)
    }
    origin = (
        "synthetic" if origins == {True} else "mixed" if len(origins) > 1 else "supplied_unknown"
    )
    evaluation = AlertEvaluation.model_validate({**evaluation.model_dump(), "origin": origin})
    values["method"] += " " + evaluation.method
    values["limitations"] += evaluation.limitations
    return result("", evaluation)
