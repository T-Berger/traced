"""Shared Evidence Explorer service contract.

Publication weeks and clip membership are supplied metadata. Neither capture
time nor frame ordering can be inferred from them. All result collections are
bounded; media bytes and the full corpus never belong in Dash browser stores.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

ALL_FILTER = "__all__"
UNKNOWN_FILTER = "__unknown__"
DEFAULT_PAGE_SIZE = 12
MAX_PAGE_SIZE = 48
CONTACT_SHEET_SIZE = 12
REVIEW_STATUSES = ("draft", "reviewed", "rejected", "needs_review")
REVIEW_STATUS_LABELS = {
    "draft": "Draft",
    "reviewed": "Reviewed",
    "rejected": "Rejected",
    "needs_review": "Needs review",
}


class ReviewConflictError(ValueError):
    """A stale review or scenario version must not overwrite newer work."""


class ReviewNotFoundError(LookupError):
    """A requested review, scenario, release or evidence reference is absent."""


def normalize_filter(value: str | None) -> str | None:
    if value is None or value in ("", "all", ALL_FILTER):
        return None
    return str(value)


@dataclass(frozen=True)
class ExplorerFilters:
    query: str = ""
    week: str | None = None
    side: str | None = None
    camera: str | None = None
    image_type: str | None = None
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE

    def __post_init__(self) -> None:
        if not 1 <= self.page <= 1_000_000:
            raise ValueError("Page must be a positive integer.")
        if not 1 <= self.page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"Page size must be between 1 and {MAX_PAGE_SIZE}.")
        if len(self.query) > 200:
            raise ValueError("Search is limited to 200 characters.")


class ExplorerStore(Protocol):
    """Dictionary return shapes are documented below and covered by contract tests."""

    path: Path

    def list_releases(self) -> list[dict[str, Any]]:
        """release_id, title, dataset_root, counts, imported_at; complete releases only."""
        ...

    def get_release(self, release_id: str | None = None) -> dict[str, Any] | None:
        """None selects the most recently imported complete release."""
        ...

    def overview(self, release_id: str) -> dict[str, Any]:
        """Return release_id/title/frame_count/clip_count/video_count/detection_count,
        missing_media_count, hash_counts {total,hashed,pending,failed}, and
        filter_options {week,side,camera,image_type}: lists of supplied strings.
        Include UNKNOWN_FILTER for blank values rather than dropping unknowns.
        """
        ...

    def list_clips(self, release_id: str, filters: ExplorerFilters) -> dict[str, Any]:
        """Return {items,total,total_frames,page,page_size}.

        Filters match frame records. Items are distinct matching clip groups:
        clip_key, clip_id, video_id, frame_count (whole clip), matched_frame_count,
        representative_frame_id, weeks, sides, cameras. clip_key must distinguish
        a clip under different video IDs. Query is literal ID lookup/substring,
        not SQL wildcards or semantic search. total_frames counts matched frames.
        """
        ...

    def get_clip(
        self,
        release_id: str,
        clip_key: str,
        offset: int = 0,
        limit: int = CONTACT_SHEET_SIZE,
    ) -> dict[str, Any] | None:
        """Return clip_key/clip_id/video_id/frame_count/offset/limit/frames.

        Frames use the get_frame shape, sorted deterministically for display
        only. Contact sheets include the whole supplied clip, not only frames
        that matched Explorer filters. Bound limit to MAX_PAGE_SIZE.
        """
        ...

    def get_frame(self, release_id: str, frame_id: str) -> dict[str, Any] | None:
        """Return all supplied frame fields plus release_id, clip_key,
        relative_path (normalized under dataset_root), source_row (CSV line),
        media_available, detection_count, media_sha256 (nullable), hash_status.
        week/width_px/height_px are int or None; missing strings stay empty.
        Selected reads recheck file availability. recorded_media_sha256 preserves
        checksum history; hash_stat_matches/hash_verification describe whether
        current size/mtime matches that record, without claiming a fresh byte
        verification. A changed or missing file has no current media_sha256.
        """
        ...

    def get_detections(self, release_id: str, frame_id: str) -> list[dict[str, Any]]:
        """Supplied, unverified rows only; no inferred identities or force statistics."""
        ...


class ReviewServiceProtocol(Protocol):
    """Persistent human annotations, isolated exclusion scenarios and posting coverage.

    Scenario shapes: scenario_id/release_id/name/is_baseline/version/created_at.
    Review shapes: review_id/scenario_id/release_id/revision/title/observation/
    interpretation/uncertainty/rationale/reviewer/status/evidence_ids/created_at,
    plus eligibility {eligible,reasons} and evidence (bounded frame records).
    All source references are release-qualified. Nothing edits imported metadata.
    """

    def ensure_baseline(self, release_id: str) -> dict[str, Any]:
        """Idempotently create the release's default review workspace, explicitly."""
        ...

    def list_scenarios(self, release_id: str) -> list[dict[str, Any]]: ...

    def get_scenario(self, scenario_id: str) -> dict[str, Any]: ...

    def create_scenario(
        self,
        release_id: str,
        name: str,
        reviewer: str,
        source_scenario_id: str | None = None,
    ) -> dict[str, Any]:
        """Fork current notes into a separate scenario, preserving origin references.
        Default source is the baseline. Never modify the source scenario/history.
        """
        ...

    def save_review(
        self,
        scenario_id: str,
        *,
        title: str,
        observation: str,
        reviewer: str,
        evidence_ids: list[str],
        interpretation: str = "",
        uncertainty: str = "",
        rationale: str = "",
        status: str = "draft",
        review_id: str | None = None,
        expected_revision: int = 0,
    ) -> dict[str, Any]:
        """Append an immutable revision with optimistic concurrency.
        Reviewed needs observation, rationale and available, nonexcluded evidence.
        Drafts may have no evidence. Supplied references must always be valid.
        Rejected/needs_review do not become approved by restoring a source.
        """
        ...

    def list_reviews(
        self,
        scenario_id: str,
        *,
        status: str | None = None,
        query: str = "",
        week: str | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """Return items/total/page/page_size, using latest revisions only.
        Week filtering follows referenced frames' posting weeks, not note dates.
        """
        ...

    def get_review(self, review_id: str) -> dict[str, Any]: ...

    def review_history(
        self,
        review_id: str,
        *,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """Return items/total/page/page_size, preserving text, evidence, actor and reason."""
        ...

    def exclusion_preview(
        self,
        scenario_id: str,
        video_id: str,
        *,
        excluded: bool = True,
    ) -> dict[str, Any]:
        """Read only: scenario_version/currently_excluded/affected_review_count,
        affected_reviews (at most MAX_PAGE_SIZE summaries), video_id/excluded.
        """
        ...

    def set_source_exclusion(
        self,
        scenario_id: str,
        video_id: str,
        *,
        excluded: bool,
        reason: str,
        reviewer: str,
        expected_version: int,
    ) -> dict[str, Any]:
        """Atomic scenario event + dependent needs_review revisions.
        Baseline exclusions are rejected. Require actor/reason and preview version.
        Restoring appends an event without automatically changing review statuses.
        Return scenario_version/video_id/excluded/affected_review_count/event_id.
        """
        ...

    def exclusion_history(
        self,
        scenario_id: str,
        *,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]: ...

    def chronology(
        self,
        scenario_id: str,
        *,
        unit: str = "videos",
        status: str | None = None,
    ) -> dict[str, Any]:
        """Available evidence, never event or operational activity counts.
        Return release_id/scenario_id/unit/title/x_label/bins. Units frames/clips/videos.
        Each bin: week int|None,label,count,excluded_count,frame_count,clip_count,
        video_count,review_count,needs_review_count,excluded_video_count.
        Fill bounded gaps; preserve an unknown-week bin when relevant. Status filters
        review counts only; supplied evidence counts retain their stated scope.
        """
        ...
