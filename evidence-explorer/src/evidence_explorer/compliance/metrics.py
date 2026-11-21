"""Enforcement-evidence metrics for a compliance audit (e.g. NYS GBS § 1102 reporting).

Every metric is computed from one saved collection of model-labeled posts. Model flags are
proposals, not confirmed violations, and one capture cannot show what a platform removed
before collection. The page states those limits beside the numbers.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ..alerts.analysis import is_classified, is_third_party, violations
from ..alerts.settings import CATEGORIES, AlertSettings

BASELINE = "baseline"
SPIKE_SIGMA = 2.0  # a week is a spike when its flagged rate exceeds mean + 2σ
ROLLING_WEEKS = 4


@dataclass
class Post:
    row: dict[str, Any]
    categories: list[str]  # categories at or above the candidate score; empty = baseline

    @property
    def flagged(self) -> bool:
        return bool(self.categories)

    def get(self, key: str) -> Any:
        return self.row.get(key)


@dataclass
class Audit:
    posts: list[Post]  # classified third-party posts only
    excluded: int  # subject-owned/affiliated or unclassified rows left out
    bursts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def flagged(self) -> list[Post]:
        return [p for p in self.posts if p.flagged]

    @property
    def baseline(self) -> list[Post]:
        return [p for p in self.posts if not p.flagged]

    def in_category(self, category: str) -> list[Post]:
        return [p for p in self.posts if category in p.categories]


def build_audit(
    labels: list[dict[str, Any]], settings: AlertSettings, bursts: list[dict] | None = None
) -> Audit:
    posts, excluded = [], 0
    for row in labels:
        if not (is_classified(row) and is_third_party(row)):
            excluded += 1
            continue
        posts.append(Post(row, violations(row, settings)))
    return Audit(posts, excluded, list(bursts or []))


# --------------------------------------------------------------------------- helpers


def _num(value) -> float | None:
    return None if value is None else float(value)


def median(values) -> float | None:
    clean = [v for v in values if v is not None]
    return statistics.median(clean) if clean else None


def days_live(post: Post) -> float | None:
    """Days between posting and the capture that found the post still publicly available."""
    posted, collected = post.get("posted_at"), post.get("collected_at")
    if not (isinstance(posted, datetime) and isinstance(collected, datetime)):
        return None
    return max(0.0, (collected - posted).total_seconds() / 86400)


def engagement_rate(post: Post) -> float | None:
    """Likes, reposts and replies per recorded view."""
    views = _num(post.get("view_count"))
    if not views:
        return None
    total = sum(_num(post.get(k)) or 0 for k in ("like_count", "repost_count", "reply_count"))
    return total / views


def shares_per_1k_views(post: Post) -> float | None:
    views = _num(post.get("view_count"))
    reposts = _num(post.get("repost_count"))
    if not views or reposts is None:
        return None
    return 1000 * reposts / views


def views_per_follower(post: Post) -> float | None:
    views, followers = _num(post.get("view_count")), _num(post.get("followers_count"))
    if views is None or not followers:
        return None
    return views / followers


def week_start(ts: datetime) -> date:
    d = ts.date()
    return d - timedelta(days=d.weekday())


def ratio(a: float | None, b: float | None) -> float | None:
    return None if a is None or not b else a / b


# --------------------------------------------------------------------------- summary


def summary(audit: Audit) -> dict[str, Any]:
    flagged, baseline = audit.flagged, audit.baseline
    flagged_views = [p.get("view_count") for p in flagged if p.get("view_count") is not None]
    live = [d for p in flagged if (d := days_live(p)) is not None]
    captures = [c for p in audit.posts if isinstance(c := p.get("collected_at"), datetime)]
    med_flagged = median(p.get("view_count") for p in flagged)
    med_base = median(p.get("view_count") for p in baseline)
    weekly = weekly_series(audit)
    rates = [w["rate"] for w in weekly if w["posts"]]
    mean_rate = statistics.fmean(rates) if rates else None
    cv = statistics.pstdev(rates) / mean_rate if len(rates) > 1 and mean_rate else None
    return {
        "posts": len(audit.posts),
        "excluded": audit.excluded,
        "flagged": len(flagged),
        "flagged_share": len(flagged) / len(audit.posts) if audit.posts else None,
        "accounts": len({p.get("author_id") for p in flagged if p.get("author_id")}),
        "flagged_views": sum(flagged_views) if flagged_views else None,
        "views_unknown": len(flagged) - len(flagged_views),
        "median_days_live": median(live),
        "min_days_live": min(live) if live else None,
        "capture_start": min(captures) if captures else None,
        "capture_end": max(captures) if captures else None,
        "posted_start": min((p.get("posted_at") for p in audit.posts if p.get("posted_at")),
                            default=None),
        "posted_end": max((p.get("posted_at") for p in audit.posts if p.get("posted_at")),
                          default=None),
        "median_views_flagged": med_flagged,
        "median_views_baseline": med_base,
        "reach_index": ratio(med_flagged, med_base),
        "share_index": ratio(
            median(shares_per_1k_views(p) for p in flagged),
            median(shares_per_1k_views(p) for p in baseline),
        ),
        "volatility_cv": cv,
        "spike_weeks": sum(w["spike"] for w in weekly),
        "archived": sum(1 for p in flagged if p.get("archive_url")),
        "bursts": len(audit.bursts),
        "burst_posts": sum(b.get("post_count") or 0 for b in audit.bursts),
    }  # fmt: skip


# --------------------------------------------------------------------------- matrices


def category_matrix(audit: Audit) -> list[dict[str, Any]]:
    """Per statutory category: volume, accounts, reach and reach relative to baseline."""
    base_views = median(p.get("view_count") for p in audit.baseline)
    base_eng = median(engagement_rate(p) for p in audit.baseline)
    out = []
    for c in CATEGORIES:
        posts = audit.in_category(c)
        views = [p.get("view_count") for p in posts if p.get("view_count") is not None]
        med_views = median(views)
        med_eng = median(engagement_rate(p) for p in posts)
        out.append(
            {
                "category": c,
                "posts": len(posts),
                "accounts": len({p.get("author_id") for p in posts if p.get("author_id")}),
                "views": sum(views) if views else 0,
                "median_views": med_views,
                "max_views": max(views) if views else None,
                "reach_index": ratio(med_views, base_views),
                "engagement_index": ratio(med_eng, base_eng),
                "median_days_live": median(days_live(p) for p in posts),
            }
        )
    return out


def periods(audit: Audit) -> list[dict[str, Any]]:
    """Flagged volume by calendar month and category, labeled with its reporting quarter."""
    buckets: dict[str, dict[str, Any]] = {}
    for p in audit.posts:
        ts = p.get("posted_at")
        if not isinstance(ts, datetime):
            continue
        key = f"{ts:%b %Y} (Q{(ts.month - 1) // 3 + 1})"
        b = buckets.setdefault(
            key,
            {"period": key, "sort": (ts.year, ts.month), "posts": 0, **{c: 0 for c in CATEGORIES}},
        )
        b["posts"] += 1
        for c in p.categories:
            b[c] += 1
    return sorted(buckets.values(), key=lambda b: b["sort"])


def weekly_series(audit: Audit) -> list[dict[str, Any]]:
    """Weekly velocity (flagged posts, flagged views) and volatility (flagged rate band)."""
    weeks: dict[date, dict[str, Any]] = {}
    for p in audit.posts:
        ts = p.get("posted_at")
        if not isinstance(ts, datetime):
            continue
        w = weeks.setdefault(
            week_start(ts),
            {"week": week_start(ts), "posts": 0, "flagged": 0, "flagged_views": 0,
             "flagged_list": [], "baseline_list": [], **{c: 0 for c in CATEGORIES}},
        )  # fmt: skip
        w["posts"] += 1
        views = p.get("view_count")
        if p.flagged:
            w["flagged"] += 1
            w["flagged_views"] += views or 0
            w["flagged_list"].append(views)
            for c in p.categories:
                w[c] += 1
        else:
            w["baseline_list"].append(views)
    series = [weeks[k] for k in sorted(weeks)]
    for w in series:
        w["rate"] = w["flagged"] / w["posts"] if w["posts"] else 0.0
        w["median_flagged_views"] = median(w.pop("flagged_list"))
        w["median_baseline_views"] = median(w.pop("baseline_list"))
    rates = [w["rate"] for w in series]
    mean = statistics.fmean(rates) if rates else 0.0
    sd = statistics.pstdev(rates) if len(rates) > 1 else 0.0
    for i, w in enumerate(series):
        window = rates[max(0, i - ROLLING_WEEKS + 1) : i + 1]
        w["rolling_mean"] = statistics.fmean(window)
        w["rolling_sd"] = statistics.pstdev(window) if len(window) > 1 else 0.0
        w["spike"] = bool(sd) and w["rate"] > mean + SPIKE_SIGMA * sd
    return series


def view_samples(audit: Audit) -> dict[str, list[float]]:
    """Recorded views for flagged and baseline posts (for distribution curves)."""
    return {
        "flagged": sorted(
            float(v) for p in audit.flagged if (v := p.get("view_count")) is not None
        ),
        BASELINE: sorted(
            float(v) for p in audit.baseline if (v := p.get("view_count")) is not None
        ),
    }


def ecdf(values: list[float]) -> tuple[list[float], list[float]]:
    """Share of posts with at most x views; values must be sorted."""
    n = len(values)
    return values, [(i + 1) / n for i in range(n)]


def persistence(audit: Audit) -> list[float]:
    return [d for p in audit.flagged if (d := days_live(p)) is not None]


def top_accounts(audit: Audit, limit: int = 10) -> list[dict[str, Any]]:
    by: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"posts": 0, "views": 0, "categories": set()}
    )
    for p in audit.flagged:
        key = p.get("author_id") or p.get("handle")
        if not key:
            continue
        a = by[key]
        a["handle"] = p.get("handle")
        a["followers"] = p.get("followers_count")
        a["verified"] = p.get("is_verified")
        a["posts"] += 1
        a["views"] += p.get("view_count") or 0
        a["categories"].update(p.categories)
    rows = [{**v, "categories": sorted(v["categories"])} for v in by.values()]
    return sorted(rows, key=lambda r: (-r["views"], -r["posts"]))[:limit]


def high_velocity(audit: Audit, min_views: int) -> list[Post]:
    """Flagged posts at or above the Alerts "high recorded views" setting."""
    return [p for p in audit.flagged if (p.get("view_count") or 0) >= min_views]


def at_review_score(audit: Audit, settings: AlertSettings) -> list[Post]:
    """Flagged posts whose flagged category also meets the stricter evidence-review score."""
    return [
        p
        for p in audit.flagged
        if any((p.get(c) or 0) >= settings.categories[c].filing_score for c in p.categories)
    ]


def top_posts(posts: list[Post], limit: int = 3) -> list[Post]:
    return sorted(posts, key=lambda p: -(p.get("view_count") or 0))[:limit]


def best_category(post: Post) -> str:
    return max(post.categories, key=lambda c: post.get(c) or 0)


def account_coverage(audit: Audit) -> list[dict[str, Any]]:
    """Accounts by how many of the four statutory categories their flagged posts span.

    Sorted by coverage (4/4 first), then flagged posts, then views.
    """
    by: dict[str, dict[str, Any]] = {}
    for p in audit.flagged:
        key = p.get("author_id") or p.get("handle")
        if not key:
            continue
        a = by.setdefault(
            key,
            {"handle": p.get("handle"), "followers": p.get("followers_count"), "posts": 0,
             "views": 0, **{c: 0 for c in CATEGORIES}},
        )  # fmt: skip
        a["posts"] += 1
        a["views"] += p.get("view_count") or 0
        for c in p.categories:
            a[c] += 1
    rows = []
    for a in by.values():
        a["coverage"] = sum(1 for c in CATEGORIES if a[c])
        rows.append(a)
    return sorted(rows, key=lambda r: (-r["coverage"], -r["posts"], -r["views"]))


def coverage_tiers(accounts: list[dict[str, Any]]) -> dict[int, int]:
    """Number of accounts at each coverage level, 4 down to 1."""
    return {
        k: sum(1 for a in accounts if a["coverage"] == k) for k in range(len(CATEGORIES), 0, -1)
    }
