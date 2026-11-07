"""Evidence items, grouped by evidence type, as plain data.

pdf_report.build_pdf renders these into the evidence report and embeds them (with the native
captures) as a JSON manifest.
"""

from __future__ import annotations

import hashlib

from .analysis import STATUS_LABELS, Assessment
from .settings import CATEGORY_LABELS

PREPARER_STATEMENT = (
    "This report was prepared from records collected and preserved as described in Section 3. "
    "The SHA-256 values in Section 6 were computed from the attached records when this report "
    "was generated, and each matches the value recorded at assessment."
)
REVIEW_NOTE = (
    "Classifications are model-assisted assessments with a saved rationale. A reviewer confirms "
    "each evidence item before it is reported to the platform."
)


def _iso(value) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else value


def evidence_item(a: Assessment, references: dict[str, dict], capture: str | None) -> dict:
    row = a.row
    # The subject's own posts are contextual evidence for false claims only.
    disinfo = a.category == "disinformation"
    refs = (
        [references[r] for r in row.get("reference_ids") or [] if r in references]
        if disinfo
        else []
    )
    capture_hash = row.get("capture_sha256")
    if capture is not None:
        computed = hashlib.sha256(capture.encode("utf-8")).hexdigest()
        if capture_hash and computed != capture_hash:
            raise ValueError(f"Capture for {row['id']} changed since it was assessed.")
        capture_hash = computed
    return {
        "id": row["id"],
        "status": STATUS_LABELS[a.status],
        "content": {
            "platform": row.get("platform"),
            "url": row.get("url"),
            "post_id": row.get("platform_item_id"),
            "posted_at": _iso(row.get("posted_at")),
            "language": row.get("lang"),
            "text": row.get("text"),
        },
        "attribution": {
            "handle": row.get("handle"),
            "user_id": row.get("platform_user_id"),
            "display_name": row.get("display_name"),
            "followers": row.get("followers_count"),
            "verified": row.get("is_verified"),
        },
        "preservation": {
            "collected_at": _iso(row.get("collected_at")),
            "capture_sha256": capture_hash,
            "capture_file": f"captures/{row['id']}.json" if capture is not None else None,
            "archive_url": row.get("archive_url"),
        },
        "classification": {
            "category": CATEGORY_LABELS[a.category],
            "all_categories": [CATEGORY_LABELS[c] for c in a.categories],
            "platform_rule": a.route["rule"],
            "explanation": row.get("rationale"),
            "claim": row.get("claim_summary") if disinfo else None,
            "protected_group": row.get("racism_target")
            or ", ".join(b.replace("_", " ") for b in row.get("hate_speech_basis") or []),
            "score": round(a.score, 3),
            "model": row.get("model"),
            "prompt_version": row.get("prompt_version"),
        },
        "context": {
            "verdict": row.get("disinfo_verdict") if disinfo else None,
            "subject_posts": [
                {"url": r.get("url"), "posted_at": _iso(r.get("posted_at")), "text": r.get("text")}
                for r in refs
            ],
        },
        "reach": {
            "views": row.get("view_count"),
            "likes": row.get("like_count"),
            "reposts": row.get("repost_count"),
            "replies": row.get("reply_count"),
        },
        "pattern": {
            "target": row.get("harassment_target") if a.category == "harassment" else None,
            "repeated_targeting": (row.get("harassment_volume") or 0) > 0,
        },
        "checks": [
            {"element": c.label, "met": c.ok, "required": c.required, "detail": c.detail}
            for c in a.checks
        ],
    }
