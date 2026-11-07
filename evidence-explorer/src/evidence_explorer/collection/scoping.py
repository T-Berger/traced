"""Pure previews and immutable cases for an explicitly selected collection scope.

The repository supplies one consistent saved snapshot. These projections never
collect sources, schedule jobs, or alter an existing case or assessment.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..review.contracts import EvidenceBundle
from ..source_bundles import build_source_bundle
from ..source_contracts import Platform, SourceItem
from .models import CollectionFailure

SCOPE_VERSION = "1.1"
ContentScope = Literal[
    "post_text",
    "title_description",
    "article_text",
    "feed_content",
    "feed_summary",
    "unknown",
    "database_row",
]
_CONTENT_LABELS = {
    "post_text": "saved post text",
    "title_description": "video titles and descriptions, not transcripts",
    "article_text": "saved article text",
    "feed_content": "text supplied by a feed",
    "feed_summary": "feed summaries, not complete articles",
    "unknown": "unknown content scope",
    "database_row": "saved database rows, with original publication context unverified",
}
_VERSION_POLICIES = {
    "one_saved_version_per_native_identity",
    "latest_selected_observation_then_source_id",
}
_HASH_PATTERN = r"^[0-9a-f]{64}$"
EMPTY_SELECTION_REASON = (
    "No saved records are available in this selection. "
    "Collect one of these sources before creating a case."
)


def _observation_time(value: str | None) -> str | None:
    if value is None:
        return None
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Saved observation times require a timezone.")
    return moment.astimezone(UTC).isoformat()


class _ObservationManifest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True, hide_input_in_errors=True)

    source_id: str = Field(min_length=1, max_length=100)
    item_key: str = Field(min_length=1, max_length=512)
    payload_hash: str | None = Field(pattern=_HASH_PATTERN)
    semantic_hash: str | None = Field(pattern=_HASH_PATTERN)
    content_hash: str | None = Field(pattern=_HASH_PATTERN)
    first_observed_at: str | None = Field(max_length=100)
    observed_at: str | None = Field(max_length=100)
    attribution: Literal["observed", "unknown"]

    _normalize_times = field_validator("first_observed_at", "observed_at")(_observation_time)

    @model_validator(mode="after")
    def known_observations_are_complete(self):
        if self.attribution == "observed":
            if any(
                value is None
                for value in (
                    self.payload_hash,
                    self.semantic_hash,
                    self.content_hash,
                    self.first_observed_at,
                    self.observed_at,
                )
            ):
                raise ValueError("Known source observations require hashes and capture times.")
            if datetime.fromisoformat(self.first_observed_at) > datetime.fromisoformat(
                self.observed_at
            ):
                raise ValueError("Saved observation capture times are inconsistent.")
        elif any(
            value is not None
            for value in (self.payload_hash, self.semantic_hash, self.content_hash)
        ):
            raise ValueError("Unknown source provenance cannot supply observed payload hashes.")
        return self


class _VersionOmission(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True, hide_input_in_errors=True)

    item_key: str = Field(min_length=1, max_length=512)
    channel_id: str = Field(min_length=1, max_length=512)
    native_id: str = Field(min_length=1, max_length=512)
    observed_at: str = Field(min_length=1, max_length=100)
    payload_hash: str = Field(pattern=_HASH_PATTERN)
    content_hash: str = Field(pattern=_HASH_PATTERN)
    included_source_id: str = Field(min_length=1, max_length=100)
    included_observed_at: str = Field(min_length=1, max_length=100)
    included_payload_hash: str = Field(pattern=_HASH_PATTERN)
    included_content_hash: str = Field(pattern=_HASH_PATTERN)
    observation_time_tied: bool
    included_in_case: bool
    reason: str = Field(min_length=1, max_length=1000)

    _normalize_times = field_validator("observed_at", "included_observed_at")(_observation_time)


class _ScopeSource(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True, hide_input_in_errors=True)

    id: str = Field(min_length=1, max_length=100)
    platform: Platform
    label: str = Field(min_length=1, max_length=150)
    locator: str = Field(min_length=1, max_length=2000)
    item_count: int = Field(ge=0)
    included_count: int = Field(ge=0)
    unknown_publication_times: int = Field(ge=0)
    content_scopes: dict[ContentScope, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    unattributed_count: int = Field(default=0, ge=0)
    omitted_version_count: int = Field(default=0, ge=0)
    version_omissions: list[_VersionOmission] = Field(default_factory=list)
    ready: bool | None = None
    connection_status: str | None = Field(default=None, max_length=1000)
    last_status: str = Field(default="unknown", min_length=1, max_length=30)
    last_error: str | None = Field(default=None, max_length=1000)
    last_collected_at: str | None = Field(default=None, max_length=100)
    item_limit: int | None = Field(default=None, ge=1, le=20)
    content_mode: str | None = Field(default=None, max_length=40)

    @model_validator(mode="before")
    @classmethod
    def missing_content_scope_is_explicitly_unknown(cls, value):
        if isinstance(value, dict) and value.get("content_scopes") is None:
            included = value.get("included_count", 0)
            return {**value, "content_scopes": {"unknown": included} if included else {}}
        return value

    @model_validator(mode="after")
    def counts_are_consistent(self):
        if (
            self.included_count + self.unattributed_count + self.omitted_version_count
            > self.item_count
        ):
            raise ValueError("Included source count exceeds the saved source count.")
        if self.unknown_publication_times > self.included_count:
            raise ValueError("Unknown publication count exceeds the included source count.")
        if sum(self.content_scopes.values()) != self.included_count:
            raise ValueError("Content scope counts must describe the included source records.")
        return self


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _source_ids(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)) or not values:
        raise CollectionFailure("Choose at least one saved source for this case.")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise CollectionFailure("The saved source selection is invalid. Choose the sources again.")
    ids = [value.strip() for value in values]
    if len(ids) != len(set(ids)):
        raise CollectionFailure("Choose each source only once.")
    return sorted(ids)


def _prepare(snapshot: dict) -> dict:
    if not isinstance(snapshot, dict):
        raise CollectionFailure(
            "The saved source snapshot is invalid. Preview the selection again."
        )
    raw_sources, raw_items = snapshot.get("sources"), snapshot.get("items")
    if not isinstance(raw_sources, list) or not isinstance(raw_items, list):
        raise CollectionFailure("The saved source snapshot is incomplete. Preview it again.")
    try:
        sources = sorted(
            (_ScopeSource.model_validate(row) for row in raw_sources), key=lambda row: row.id
        )
        items = [SourceItem.model_validate(item) for item in raw_items]
        manifest = snapshot.get("observation_manifest")
        if manifest is not None:
            if not isinstance(manifest, list):
                raise TypeError("Observation manifest must be a list.")
            manifest = sorted(
                (_ObservationManifest.model_validate(row) for row in manifest),
                key=lambda row: (row.source_id, row.item_key),
            )
    except (ValidationError, TypeError):
        raise CollectionFailure(
            "The saved source snapshot contains invalid records or coverage. Preview it again."
        ) from None
    ids = _source_ids(snapshot.get("source_ids", [source.id for source in sources]))
    row_ids = [source.id for source in sources]
    if len(row_ids) != len(set(row_ids)):
        raise CollectionFailure("The saved source snapshot contains duplicate source identifiers.")
    if ids != row_ids:
        raise CollectionFailure(
            "One or more selected sources are no longer available. Refresh the source list."
        )
    if any(
        omission.included_source_id not in ids
        for source in sources
        for omission in source.version_omissions
    ):
        raise CollectionFailure("A preferred saved observation is outside the selected scope.")
    if manifest is not None:
        if any(row.source_id not in ids for row in manifest):
            raise CollectionFailure("A saved observation is outside the selected scope.")
        manifest_keys = [(row.source_id, row.item_key) for row in manifest]
        if len(manifest_keys) != len(set(manifest_keys)):
            raise CollectionFailure("The saved observation manifest contains duplicate entries.")
    by_identity = {}
    for item in items:
        identity = (item.platform, item.channel_id, item.native_id)
        payload = item.model_dump(mode="json")
        if identity in by_identity and by_identity[identity] != payload:
            raise CollectionFailure("The saved selection contains conflicting source records.")
        by_identity[identity] = payload
    saved_items = [payload for _, payload in sorted(by_identity.items())]
    if len(saved_items) > 2000:
        raise CollectionFailure("A selected case can include at most 2,000 saved records.")
    total = snapshot.get("total_items", len(saved_items))
    if isinstance(total, bool) or not isinstance(total, int) or total < len(saved_items):
        raise CollectionFailure("The selected record coverage is invalid. Preview it again.")
    truncated = snapshot.get("truncated", total > len(saved_items))
    if not isinstance(truncated, bool):
        raise CollectionFailure("The selected record coverage is invalid. Preview it again.")
    if any(source.included_count > len(saved_items) for source in sources):
        raise CollectionFailure("The per-source coverage does not match the saved selection.")
    version_policy = snapshot.get("version_policy", "one_saved_version_per_native_identity")
    if not isinstance(version_policy, str) or version_policy not in _VERSION_POLICIES:
        raise CollectionFailure("The saved record selection policy is invalid. Preview it again.")
    observations_fingerprint = snapshot.get("observations_fingerprint")
    if observations_fingerprint is not None and (
        not isinstance(observations_fingerprint, str)
        or len(observations_fingerprint) != 64
        or any(char not in "0123456789abcdef" for char in observations_fingerprint)
    ):
        raise CollectionFailure("The saved observation fingerprint is invalid. Preview it again.")
    rows = [source.model_dump(mode="json") for source in sources]
    for row in rows:
        row["version_omissions"].sort(
            key=lambda omission: json.dumps(omission, sort_keys=True, separators=(",", ":"))
        )
    return {
        "scope_version": SCOPE_VERSION,
        "source_ids": ids,
        "sources": rows,
        "items": saved_items,
        "total_items": total,
        "truncated": truncated or total > len(saved_items),
        "version_policy": version_policy,
        "observations_fingerprint": observations_fingerprint,
        "observation_manifest": (
            [row.model_dump(mode="json") for row in manifest] if manifest is not None else None
        ),
    }


def _scope(prepared: dict) -> dict:
    sources, items = prepared["sources"], prepared["items"]
    fingerprint = _digest(prepared)
    names = [source["label"] for source in sources]
    title = "Collected evidence · " + " + ".join(names[:2])
    if len(names) > 2:
        title += f" + {len(names) - 2} more"
    limitations = [
        "Selected sources define this case's collection scope; selection is not an "
        "analytical exclusion or a change to an earlier assessment.",
        "Only saved records from the selected sources are included. Full source coverage "
        "and independence are not established.",
        "Connection readiness records access setup separately from the latest collection history.",
    ]
    if prepared["version_policy"] == "latest_selected_observation_then_source_id":
        limitations.append(
            "For each native record, the case prefers the newest saved observation among "
            "selected sources, using source ID to break ties. This version choice does not "
            "establish the newest platform edit, exact duplication, or independent corroboration. "
            "A source-ID tie break is a selection convention for equal saved observation times."
        )
    if any(source["omitted_version_count"] for source in sources):
        limitations.append(
            "Omission metadata distinguishes hashes of full saved observation payloads from "
            "hashes of exact saved text. Different payload hashes can reflect metadata differences "
            "and do not prove that text was edited. Omitted text and content are not included or "
            "available as readable evidence in this case or its package."
        )
    for source in sources:
        material = "; ".join(
            f"{count} record(s): {_CONTENT_LABELS[kind]}"
            for kind, count in sorted(source["content_scopes"].items())
            if count
        )
        if material:
            limitations.append(f"{source['label']}: included content scope — {material}.")
        if source["unattributed_count"]:
            limitations.append(
                f"{source['label']}: {source['unattributed_count']} saved record(s) lack "
                "durable source provenance and were not included. No attribution was inferred."
            )
        if source["omitted_version_count"]:
            limitations.append(
                f"{source['label']}: {source['omitted_version_count']} differing saved version(s) "
                "were omitted under the declared native-record version policy. The preferred "
                "observation is not included as evidence when it also falls outside the case limit."
            )
        if source["ready"] is False:
            limitations.append(
                f"{source['label']}: current connection setup is unavailable; "
                "any saved collection history remains separate."
            )
        elif source["ready"] is None:
            limitations.append(f"{source['label']}: current connection readiness was not recorded.")
        if source["item_count"] == 0:
            limitations.append(f"{source['label']}: no saved records were available for this case.")
        if source["last_status"] == "failed":
            limitations.append(
                f"{source['label']}: the latest collection failed; any included records are "
                "from an earlier saved collection."
            )
    unknown = sum(item["published_at"] is None for item in items)
    if unknown:
        limitations.append(
            f"{unknown} included record(s) have unknown publication times; acquisition "
            "times are not substituted."
        )
    if prepared["truncated"]:
        limitations.append(
            f"This case includes {len(items)} of {prepared['total_items']} saved records "
            "in the selected scope; additional records were omitted by the case limit."
        )
    complete_observation_provenance = all(
        len(source["version_omissions"]) == source["omitted_version_count"] for source in sources
    ) and (
        bool(prepared["observations_fingerprint"])
        or not (
            prepared["truncated"]
            or any(
                source["omitted_version_count"] or source["unattributed_count"]
                for source in sources
            )
        )
    )
    if prepared["version_policy"] == "latest_selected_observation_then_source_id":
        manifest = prepared["observation_manifest"]
        complete_observation_provenance = (
            complete_observation_provenance
            and bool(prepared["observations_fingerprint"])
            and manifest is not None
            and all(
                sum(row["source_id"] == source["id"] for row in manifest) == source["item_count"]
                and sum(
                    row["source_id"] == source["id"] and row["attribution"] == "unknown"
                    for row in manifest
                )
                == source["unattributed_count"]
                for source in sources
            )
        )
    declared_version_policy = prepared[
        "version_policy"
    ] == "latest_selected_observation_then_source_id" or not any(
        source["omitted_version_count"] for source in sources
    )
    can_create = bool(items) and complete_observation_provenance and declared_version_policy
    reason = ""
    if not items:
        reason = EMPTY_SELECTION_REASON
    elif not can_create:
        reason = (
            "The selected saved observations have incomplete version or provenance details. "
            "Preview the selection again before creating a case."
        )
        limitations.append(reason)
    return {
        "scope_version": SCOPE_VERSION,
        "fingerprint": fingerprint,
        "source_ids": prepared["source_ids"],
        "sources": sources,
        "title": title,
        "source_count": len(sources),
        "record_count": len(items),
        "total_items": prepared["total_items"],
        "unknown_publication_times": unknown,
        "truncated": prepared["truncated"],
        "version_policy": prepared["version_policy"],
        "observations_fingerprint": prepared["observations_fingerprint"],
        "observation_manifest": prepared["observation_manifest"],
        "limitations": limitations,
        "can_create": can_create,
        "reason": reason,
    }


def preview_scope(snapshot: dict) -> dict:
    """Describe an exact saved scope, including failed and empty selected sources."""
    return _scope(_prepare(snapshot))


def scoped_bundle(snapshot: dict) -> EvidenceBundle:
    """Build a new immutable assessment context from selected saved evidence only."""
    prepared = _prepare(snapshot)
    scope = _scope(prepared)
    if not scope["can_create"]:
        raise CollectionFailure(scope["reason"])
    base = build_source_bundle(
        {
            "source_schema": "1.0",
            "case": {
                "title": scope["title"],
                "question": "What do the saved records from the selected sources establish?",
                "synthetic": False,
            },
            "items": prepared["items"],
        }
    ).model_dump(mode="json")
    fingerprint = scope["fingerprint"]
    base["case"]["id"] = "sources:scope:" + fingerprint
    base["case"]["coverage"]["collection_scope"] = deepcopy(scope)
    base["case"]["limitations"].extend(scope["limitations"])
    base["collection_scope"] = deepcopy(scope)
    runs = {
        run["id"]: "sources:scope-run:" + _digest([fingerprint, run["id"]]) for run in base["runs"]
    }
    findings = {
        finding["snapshot_id"]: "sources:scope-finding:"
        + _digest([fingerprint, finding["snapshot_id"]])
        for finding in base["findings"]
    }
    for run in base["runs"]:
        original_id = run["id"]
        run["id"] = runs[original_id]
        run["input_fingerprint"] = _digest([fingerprint, run["input_fingerprint"]])
        run["configuration"]["collection_scope_fingerprint"] = fingerprint
        run["configuration"]["selected_source_ids"] = scope["source_ids"]
        run["configuration"]["native_version_policy"] = scope["version_policy"]
        run["configuration"]["selected_observations_fingerprint"] = scope[
            "observations_fingerprint"
        ]
        run["configuration"]["content_scope_source"] = "included_saved_record_acquisition"
        run["coverage"]["collection_scope"] = deepcopy(scope)
    for finding in base["findings"]:
        finding["snapshot_id"] = findings[finding["snapshot_id"]]
        finding["lineage_id"] = "sources:scope-lineage:" + _digest(
            [fingerprint, finding["lineage_id"]]
        )
        finding["run_id"] = runs[finding["run_id"]]
        finding["details"]["collection_scope_fingerprint"] = fingerprint
    for link in base["links"]:
        link["id"] = "sources:scope-link:" + _digest([fingerprint, link["id"]])
        if link.get("run_id"):
            link["run_id"] = runs[link["run_id"]]
        for reference in (link["source"], link["target"]):
            if reference["type"] == "finding":
                reference["id"] = findings[reference["id"]]
    return EvidenceBundle.model_validate(base)
