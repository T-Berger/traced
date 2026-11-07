"""Reporting readiness: does a labeled post carry every type of evidence a report needs?

Evidence types collected for each post:
- Content evidence: the post as published, with its exact URL and post ID.
- Attribution evidence: the account that posted it (handle and platform user ID).
- Preservation evidence: the native platform record, collection time and SHA-256 hash.
- Classification evidence: the X rule it breaks, the reason, and the assessment score.
- Contextual evidence: for false claims, the subject's own verified posts that contradict them.
- Reach evidence: engagement at collection.
- Pattern evidence: repeated posting at one target, or repeated violations by one account.

Preservation follows the Berkeley Protocol on Digital Open Source Investigations (capture, time,
hash, chain of custody). A post is READY when every required check passes, NEEDS EVIDENCE when
the violation is at reporting confidence but a required check fails, and MONITOR when it is a
violation below reporting confidence. Labels are model-assisted; a reviewer confirms each post.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .settings import CATEGORIES, CATEGORY_LABELS, AlertSettings

SUBJECT_ATTRIBUTIONS = {"subject_own", "subject_affiliated"}
CONTRADICTED = {"contradicted_by_subject", "contradicted_by_credible_source"}

READY, NEEDS, MONITOR = "ready", "needs_evidence", "monitor"
STATUS_LABELS = {READY: "Ready to report", NEEDS: "Needs evidence", MONITOR: "Monitor"}

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
    ("Content evidence", "The post as published, verbatim, with its exact URL and post ID."),
    ("Attribution evidence", "The account that posted it: handle, platform user ID, profile."),
    ("Preservation evidence", "The native platform record captured at collection, its "
     "collection time and SHA-256 hash."),
    ("Classification evidence", "The X rule the post breaks, the reason, and the assessment "
     "score."),
    ("Contextual evidence", "For false claims: the subject's own verified posts that "
     "contradict them."),
    ("Reach evidence", "Views, likes, reposts and replies at the time of collection."),
    ("Pattern evidence", "Repeated posting at one target, or repeated violations by one "
     "account."),
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


def _checks(row: dict, category: str, settings: AlertSettings) -> list[Check]:
    score = row.get(category) or 0
    filing = settings.categories[category].filing_score
    checks = [
        Check(
            "violation",
            f"{CATEGORY_LABELS[category]} at reporting confidence",
            score >= filing,
            detail=f"score {score:.2f}, reporting standard {filing:.2f}",
        ),
        Check(
            "location",
            "Exact post URL and ID",
            bool(row.get("url") and row.get("platform_item_id")),
            detail=row.get("url") or "",
        ),
        Check(
            "author",
            "Author account identified",
            bool(row.get("handle") and row.get("platform_user_id")),
            detail=f"@{row['handle']} · user id {row.get('platform_user_id')}"
            if row.get("handle")
            else "",
        ),
        Check(
            "capture",
            "Preserved capture: timestamp and SHA-256",
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
                "Falsity shown by the subject's own record",
                row.get("disinfo_verdict") in CONTRADICTED and bool(refs),
                detail="contradicted by the subject's verified posts"
                if row.get("disinfo_verdict") in CONTRADICTED
                else "claim not contradicted by the record (unverified)",
            )
        )
    elif category in ("racism", "hate_speech"):
        basis = row.get("hate_speech_basis") or []
        target = row.get("racism_target") or ", ".join(b.replace("_", " ") for b in basis)
        checks.append(
            Check(
                "proof",
                "Protected group targeted",
                bool(target),
                detail=f"targets: {target}" if target else "no protected group identified",
            )
        )
    else:
        checks.append(
            Check(
                "proof",
                "Repeated targeting of one person",
                (row.get("harassment_volume") or 0) > 0 and bool(row.get("harassment_target")),
                detail=f"target: {row.get('harassment_target') or 'unknown'}",
            )
        )
    checks.append(
        Check(
            "archive",
            "Independent archive copy",
            bool(row.get("archive_url")),
            required=settings.require_archive_copy,
            detail=row.get("archive_url") or "none saved; recommended before reporting",
        )
    )
    return checks


def assess(row: dict, settings: AlertSettings) -> Assessment | None:
    """Best readiness across the post's violation categories, or None if not a violation."""
    if not is_third_party(row):
        return None
    cats = violations(row, settings)
    if not cats:
        return None
    rank = {READY: 0, NEEDS: 1, MONITOR: 2}
    best: Assessment | None = None
    for c in cats:
        checks = _checks(row, c, settings)
        violation_ok = checks[0].ok
        missing = [x for x in checks if x.required and not x.ok]
        status = READY if not missing else NEEDS if violation_ok else MONITOR
        a = Assessment(row, status, c, row.get(c) or 0, checks, route_for(row, c), cats)
        if best is None or (rank[a.status], -a.score) < (rank[best.status], -best.score):
            best = a
    return best


def assess_all(rows: list[dict], settings: AlertSettings) -> list[Assessment]:
    out = [a for r in rows if (a := assess(r, settings))]
    rank = {READY: 0, NEEDS: 1, MONITOR: 2}
    return sorted(out, key=lambda a: (rank[a.status], -a.score, -(a.row.get("view_count") or 0)))


def accounts(
    assessments: list[Assessment], settings: AlertSettings, bursts: list[dict]
) -> list[dict]:
    """Accounts behind violations; READY when enough of their posts are ready to file."""
    by_author: dict[str, list[Assessment]] = defaultdict(list)
    for a in assessments:
        if a.row.get("author_id"):
            by_author[a.row["author_id"]].append(a)
    burst_count: dict[str, int] = defaultdict(int)
    for b in bursts:
        if b.get("author_handle"):
            burst_count[b["author_handle"].lower()] += 1
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
                "bursts": burst_count.get((row.get("handle") or "").lower(), 0),
                "views": sum(a.row.get("view_count") or 0 for a in items),
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


def verdict(assessments: list[Assessment], account_rows: list[dict]) -> dict:
    """The one message at the top of the page."""
    ready = [a for a in assessments if a.status == READY]
    needs = [a for a in assessments if a.status == NEEDS]
    ready_accounts = [x for x in account_rows if x["status"] == READY]
    if ready:
        rules = sorted({a.route["rule"] for a in ready})
        return {
            "level": "red",
            "headline": f"{len(ready)} post{'s' if len(ready) != 1 else ''} ready for a "
            "takedown report",
            "detail": "Every required type of evidence is present. Platform rules: "
            + "; ".join(rules)
            + (
                f". {len(ready_accounts)} account{'s' if len(ready_accounts) != 1 else ''} "
                "also meet the account-report standard."
                if ready_accounts
                else "."
            ),
        }
    if needs:
        common: dict[str, int] = defaultdict(int)
        for a in needs:
            for m in a.missing:
                common[m.label] += 1
        top = max(common, key=common.get)
        return {
            "level": "amber",
            "headline": "Not ready to report yet",
            "detail": f"{len(needs)} strong violation{'s' if len(needs) != 1 else ''} "
            f"found, but evidence is incomplete. Most often missing: {top.lower()}.",
        }
    return {
        "level": "clear",
        "headline": "No reportable violations",
        "detail": "None of the labeled posts reach the evidence standard."
        if not assessments
        else f"{len(assessments)} lower-confidence violations are being monitored.",
    }
