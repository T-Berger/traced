"""Exercise-week posting coverage with separate human-review counts."""

from __future__ import annotations

from .contracts import REVIEW_STATUSES, ReviewNotFoundError, normalize_filter
from .storage import Store

MAX_WEEK_BINS = 256
UNITS = {"frames": "frame_count", "clips": "clip_count", "videos": "video_count"}
EXCLUDED_UNITS = {
    "frames": "excluded_frame_count",
    "clips": "excluded_clip_count",
    "videos": "excluded_video_count",
}


def chronology(store: Store, scenario_id: str, *, unit: str = "videos", status: str | None = None):
    if unit not in UNITS:
        raise ValueError("Chronology unit must be frames, clips, or videos.")
    status = normalize_filter(status)
    if status is not None and status not in REVIEW_STATUSES:
        raise ValueError("Choose Draft, Reviewed, Rejected, or Needs review.")
    with store.connection() as conn:
        scenario = conn.execute(
            "SELECT release_id FROM review_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if scenario is None:
            raise ReviewNotFoundError("Scenario not found.")
        release_id = scenario["release_id"]
        evidence_rows = conn.execute(
            "SELECT f.week,COUNT(*) frame_count,COUNT(DISTINCT f.clip_key) clip_count,"
            "COUNT(DISTINCT NULLIF(f.video_id,'')) video_count,"
            "SUM(CASE WHEN x.excluded=1 THEN 1 ELSE 0 END) excluded_frame_count,"
            "COUNT(DISTINCT CASE WHEN x.excluded=1 THEN f.clip_key END) excluded_clip_count,"
            "COUNT(DISTINCT CASE WHEN x.excluded=1 THEN NULLIF(f.video_id,'') END) excluded_video_count "
            "FROM frames f LEFT JOIN source_exclusions x ON x.scenario_id=? "
            "AND x.release_id=f.release_id AND x.video_id=f.video_id AND x.excluded=1 "
            "WHERE f.release_id=? GROUP BY f.week",
            (scenario_id, release_id),
        ).fetchall()
        args = [scenario_id]
        condition = "c.scenario_id=?"
        if status is not None:
            condition += " AND c.status=?"
            args.append(status)
        review_rows = conn.execute(
            "SELECT f.week,COUNT(DISTINCT c.review_id) review_count,"
            "COUNT(DISTINCT CASE WHEN c.status='needs_review' THEN c.review_id END) needs_review_count "
            "FROM review_current c LEFT JOIN review_evidence e ON e.review_id=c.review_id "
            "AND e.revision=c.revision LEFT JOIN frames f ON f.release_id=e.release_id "
            f"AND f.frame_id=e.frame_id WHERE {condition} GROUP BY f.week",
            args,
        ).fetchall()
    evidence = {row["week"]: dict(row) for row in evidence_rows}
    reviews = {row["week"]: dict(row) for row in review_rows}
    observed = set(evidence) | set(reviews)
    weeks = sorted(week for week in observed if week is not None)
    numeric_budget = MAX_WEEK_BINS - int(None in observed)
    if len(weeks) > numeric_budget:
        raise ValueError(
            "Too many exercise-week labels for a bounded chart; review the input schema."
        )
    gap_fill_limited = bool(weeks and weeks[-1] - weeks[0] + 1 > numeric_budget)
    if weeks and not gap_fill_limited:
        weeks = list(range(weeks[0], weeks[-1] + 1))
    labels = [*weeks, *([None] if None in observed else [])]
    bins = []
    for week in labels:
        supplied = evidence.get(week, {})
        reviewed = reviews.get(week, {})
        item = {
            "week": week,
            "label": "Unknown week" if week is None else f"Week {week}",
            **{
                field: supplied.get(field, 0)
                for field in (
                    "frame_count",
                    "clip_count",
                    "video_count",
                    "excluded_video_count",
                )
            },
            "review_count": reviewed.get("review_count", 0),
            "needs_review_count": reviewed.get("needs_review_count", 0),
            "count": supplied.get(UNITS[unit], 0),
            "excluded_count": supplied.get(EXCLUDED_UNITS[unit], 0),
        }
        bins.append(item)
    return {
        "release_id": release_id,
        "scenario_id": scenario_id,
        "unit": unit,
        "title": "Exercise week first posted",
        "x_label": "Exercise week first posted",
        "bins": bins,
        "gap_fill_limited": gap_fill_limited,
        "review_status_filter": status,
        "scope_note": (
            "Counts describe supplied evidence, including excluded sources. Excluded counts are "
            "a subset overlay. Each review appears once per referenced posting week; unsupported "
            "drafts appear under Unknown week. Posting weeks do not establish capture chronology."
        ),
    }


build_chronology = chronology
