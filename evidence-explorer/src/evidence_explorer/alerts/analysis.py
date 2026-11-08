"""Screen model-labeled posts for human evidence review.

Thresholds select candidates; they do not establish truth, policy violations or review decisions.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..review.comparison_ui import safe_source_url
from .settings import CATEGORIES, CATEGORY_LABELS, AlertSettings

SUBJECT_ATTRIBUTIONS = {"subject_own", "subject_affiliated"}
CONTRADICTED = {"contradicted_by_subject", "contradicted_by_credible_source"}

READY, NEEDS, MONITOR = "ready", "needs_evidence", "monitor"
STATUS_LABELS = {
    READY: "Candidate for review",
    NEEDS: "Needs evidence",
    MONITOR: "Below review threshold",
}

# The X rule each category is reported under.
ROUTES = {
    "disinformation": {"rule": "X Authenticity / Civic integrity rules"},
    "impersonation": {"rule": "X Misleading and deceptive identities"},
    "manipulated_media": {"rule": "X Synthetic and manipulated media"},
    "racism": {"rule": "X Hateful conduct"},
    "hate_speech": {"rule": "X Hateful conduct"},
    "harassment": {"rule": "X Abuse and harassment"},
}

EVIDENCE_TYPES = [
    ("Content evidence", "Provided extracted text, URL and post ID; source mapping needs review."),
    ("Attribution evidence", "Provided account metadata; attribution needs independent review."),
    ("Preservation evidence", "The saved record, reported collection time, and SHA-256 "
     "computed at export; origin and extraction require review."),
    ("Classification evidence", "The proposed category, rationale, policy reference, "
     "and score; not a human decision."),
    ("Contextual evidence", "Case-linked records; relevance and extracted text need review."),
    ("Reach evidence", "Views, likes, reposts and replies at the time of collection."),
    ("Pattern evidence", "Model-identified patterns that require independent review."),
]  # fmt: skip


@dataclass
class Check:
    key: str
    label: str
    ok: bool
    required: bool = True
    detail: str = ""


@dataclass
class Assessment:
    row: dict[str, Any]
    status: str
    category: str
    score: float
    checks: list[Check]
    route: dict[str, str]
    categories: list[str] = field(default_factory=list)  # every category at violation level

    @property
    def missing(self) -> list[Check]:
        return [c for c in self.checks if c.required and not c.ok]

    @property
    def id(self) -> str:
        return self.row["id"]


def is_supported_platform(row: dict) -> bool:
    return str(row.get("platform") or "").strip().lower() in {"x", "twitter"}


def is_classified(row: dict) -> bool:
    return row.get("status") == "classified"


def is_third_party(row: dict) -> bool:
    return row.get("attribution") not in SUBJECT_ATTRIBUTIONS


def violations(row: dict, settings: AlertSettings) -> list[str]:
    """Categories at or above the violation score."""
    if not is_classified(row):
        return []
    return [c for c in CATEGORIES if (row.get(c) or 0) >= settings.categories[c].label_score]


def route_for(row: dict, category: str) -> dict[str, str]:
    if category == "disinformation":
        if row.get("attribution") == "impersonation_suspect":
            return ROUTES["impersonation"]
        if (row.get("signals") or {}).get("engineered_markers"):
            return ROUTES["manipulated_media"]
    return ROUTES[category]


def _checks(
    row: dict, category: str, settings: AlertSettings, references: dict[str, dict]
) -> list[Check]:
    score = row.get(category) or 0
    filing = settings.categories[category].filing_score
    attribution = row.get("attribution")
    attribution_known = bool(
        attribution
        and str(attribution).strip().casefold()
        not in {"unknown", "unclear", "unavailable", "none", "null"}
    )
    checks = [
        Check(
            "violation",
            f"{CATEGORY_LABELS[category]} model score meets candidate threshold",
            score >= filing,
            detail=f"model score {score:.2f}, threshold {filing:.2f}; not a human decision",
        ),
        Check(
            "platform",
            "Supported X platform recorded",
            is_supported_platform(row),
            detail=f"provided platform: {row.get('platform') or 'unknown'}; "
            "other platforms require separate policy references",
        ),
        Check(
            "attribution",
            "Provided attribution available (unverified)",
            attribution_known,
            detail=f"provided attribution: {attribution}"
            if attribution_known
            else "attribution unknown; do not assume a third-party origin",
        ),
        Check(
            "text",
            "Provided extracted text available",
            bool(isinstance(row.get("text"), str) and row["text"].strip()),
            detail="Extracted text must be verified against the saved record.",
        ),
        Check(
            "location",
            "Exact post URL and ID",
            bool(safe_source_url(row.get("url")) and row.get("platform_item_id")),
            detail="A safe source link is available."
            if safe_source_url(row.get("url"))
            else "Source URL missing or unavailable.",
        ),
        Check(
            "author",
            "Provided account metadata available",
            bool(row.get("handle") and row.get("platform_user_id")),
            detail=f"@{row['handle']} · user id {row.get('platform_user_id')}"
            if row.get("handle")
            else "",
        ),
        Check(
            "capture",
            "Saved record and export hash available",
            bool(row.get("collected_at") and row.get("capture_sha256")),
            detail=f"collected {row['collected_at']:%Y-%m-%d %H:%M UTC}"
            if row.get("collected_at")
            else "",
        ),
    ]
    if category == "disinformation":
        refs = row.get("reference_ids") or []
        checks.append(
            Check(
                "proof",
                "Case-scoped context records available",
                bool(refs)
                and all(
                    r in references
                    and row.get("case_id")
                    and references[r].get("case_id") == row.get("case_id")
                    and references[r].get("id") == r
                    and references[r].get("text")
                    and isinstance(references[r].get("raw"), str)
                    and references[r].get("capture_sha256")
                    == hashlib.sha256(references[r]["raw"].encode("utf-8")).hexdigest()
                    for r in refs
                ),
                detail="Context availability does not establish falsity or verify a model verdict.",
            )
        )
    elif category in ("racism", "hate_speech"):
        basis = row.get("hate_speech_basis") or []
        target = row.get("racism_target") or ", ".join(b.replace("_", " ") for b in basis)
        checks.append(
            Check(
                "proof",
                "Model-identified target available",
                bool(target),
                detail=f"targets: {target}" if target else "no protected group identified",
            )
        )
    else:
        checks.append(
            Check(
                "proof",
                "Model-identified pattern available",
                (row.get("harassment_volume") or 0) > 0 and bool(row.get("harassment_target")),
                detail=f"target: {row.get('harassment_target') or 'unknown'}",
            )
        )
    checks.append(
        Check(
            "archive",
            "Independently saved archive copy",
            False,
            required=settings.require_archive_copy,
            detail="Archive links are not verified saved copies; check and preserve independently.",
        )
    )
    return checks


def assess(
    row: dict, settings: AlertSettings, references: dict[str, dict] | None = None
) -> Assessment | None:
    """Best readiness across the post's violation categories, or None if not a violation."""
    if not is_third_party(row):
        return None
    cats = violations(row, settings)
    if not cats:
        return None
    rank = {READY: 0, NEEDS: 1, MONITOR: 2}
    best: Assessment | None = None
    for c in cats:
        checks = _checks(row, c, settings, references or {})
        violation_ok = checks[0].ok
        missing = [x for x in checks if x.required and not x.ok]
        status = READY if not missing else NEEDS if violation_ok else MONITOR
        a = Assessment(row, status, c, row.get(c) or 0, checks, route_for(row, c), cats)
        if best is None or (rank[a.status], -a.score) < (rank[best.status], -best.score):
            best = a
    return best


def assess_all(
    rows: list[dict], settings: AlertSettings, references: dict[str, dict] | None = None
) -> list[Assessment]:
    out = [a for r in rows if (a := assess(r, settings, references))]
    rank = {READY: 0, NEEDS: 1, MONITOR: 2}
    return sorted(out, key=lambda a: (rank[a.status], -a.score, -(a.row.get("view_count") or 0)))


def accounts(
    assessments: list[Assessment], settings: AlertSettings, _bursts: list[dict]
) -> list[dict]:
    """Accounts behind violations; READY when enough of their posts are ready to file."""
    by_author: dict[str, list[Assessment]] = defaultdict(list)
    for a in assessments:
        if a.row.get("author_id"):
            by_author[a.row["author_id"]].append(a)
    out = []
    for author_id, items in by_author.items():
        row = items[0].row
        ready = [a for a in items if a.status == READY]
        status = (
            READY if len(ready) >= settings.account_ready_posts else NEEDS if ready else MONITOR
        )
        out.append(
            {
                "author_id": author_id,
                "handle": row.get("handle"),
                "platform_user_id": row.get("platform_user_id"),
                "followers": row.get("followers_count"),
                "ready": len(ready),
                "violations": len(items),
                "categories": sorted({c for a in items for c in a.categories}),
                "bursts": None,  # Handle alone does not bind a burst to a native account ID.
                "views": sum(
                    a.row["view_count"] for a in items if a.row.get("view_count") is not None
                )
                if any(a.row.get("view_count") is not None for a in items)
                else None,
                "views_unknown": sum(a.row.get("view_count") is None for a in items),
                "status": status,
            }
        )
    rank = {READY: 0, NEEDS: 1, MONITOR: 2}
    return sorted(out, key=lambda x: (rank[x["status"]], -x["ready"], -x["violations"]))


def timeline(assessments: list[Assessment]) -> list[dict]:
    """Violations per UTC day by category, plus how many were ready to file."""
    days: dict[date, dict[str, Any]] = {}
    for a in assessments:
        ts = a.row.get("posted_at")
        if not ts:
            continue
        d = days.setdefault(ts.date(), {"day": ts.date(), READY: 0, **{c: 0 for c in CATEGORIES}})
        d[a.category] += 1
        if a.status == READY:
            d[READY] += 1
    return [days[k] for k in sorted(days)]


def verdict(assessments: list[Assessment]) -> dict:
    """Summarize model candidates without assigning a human decision."""
    ready = sum(a.status == READY for a in assessments)
    needs = sum(a.status == NEEDS for a in assessments)
    lower = sum(a.status == MONITOR for a in assessments)
    return {
        "level": "amber" if assessments else "clear",
        "headline": f"{ready} candidate{'s' if ready != 1 else ''} with listed material",
        "detail": (
            f"{needs} candidates need evidence; {lower} are below the review threshold. "
            "Model scores are proposals; "
            "no human decision or policy violation is recorded here."
        ),
    }
