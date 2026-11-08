"""Evidence items, grouped by evidence type, as plain data.

pdf_report.build_pdf renders these into the evidence report and embeds them (with the native
captures) as a JSON manifest.
"""

from __future__ import annotations

import hashlib

from .analysis import STATUS_LABELS, Assessment
from .settings import CATEGORY_LABELS

PREPARER_STATEMENT = (
    "The attached saved records were captured in one database read snapshot. SHA-256 hashes "
    "were calculated from the attached bytes at export. They do not establish the collection-time "
    "contents, authenticity, or the relationship between extracted text and the native record."
)
REVIEW_NOTE = (
    "This document is a draft of model proposals and linked records. No human review decision "
    "or policy violation is established here. A reviewer must check the original records, "
    "attribution, context and current platform rules before deciding on any action."
)


def _iso(value) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else value


def evidence_item(a: Assessment, references: dict[str, dict], capture: str | None) -> dict:
    row = a.row
    # Linked records must resolve within the same case snapshot; their meaning is unreviewed.
    disinfo = a.category == "disinformation"
    ref_ids = row.get("reference_ids") or [] if disinfo else []
    refs = []
    for ref_id in ref_ids:
        ref = references.get(ref_id)
        if (
            not ref
            or ref.get("id") != ref_id
            or not row.get("case_id")
            or ref.get("case_id") != row["case_id"]
        ):
            raise ValueError(f"Linked context {ref_id} is unavailable in the selected case.")
        raw = ref.get("raw")
        recorded_hash = ref.get("capture_sha256")
        if not isinstance(raw, str) or not recorded_hash:
            raise ValueError(f"Linked context {ref_id} has no saved capture.")
        if hashlib.sha256(raw.encode("utf-8")).hexdigest() != recorded_hash:
            raise ValueError(f"Linked context {ref_id} changed since capture.")
        refs.append(ref)
    if not isinstance(capture, str) or not row.get("capture_sha256"):
        raise ValueError(f"Saved capture for {row['id']} is unavailable.")
    capture_hash = hashlib.sha256(capture.encode("utf-8")).hexdigest()
    if capture_hash != row["capture_sha256"]:
        raise ValueError(f"Capture for {row['id']} changed since assessment.")
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
            "source_attribution": row.get("attribution"),
            "handle": row.get("handle"),
            "user_id": row.get("platform_user_id"),
            "display_name": row.get("display_name"),
            "followers": row.get("followers_count"),
            "verified": row.get("is_verified"),
        },
        "preservation": {
            "collected_at": _iso(row.get("collected_at")),
            "capture_sha256": capture_hash,
            "capture_file": f"{row['id']}.json" if capture is not None else None,
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
            "score": a.score,
            "classified_at": _iso(row.get("classified_at")),
            "run_id": None,
            "model": row.get("model"),
            "prompt_version": row.get("prompt_version"),
        },
        "context": {
            "verdict": row.get("disinfo_verdict") if disinfo else None,
            "subject_posts": [
                {
                    "id": r["id"],
                    "url": r.get("url"),
                    "posted_at": _iso(r.get("posted_at")),
                    "text": r.get("text"),
                    "capture_sha256": r["capture_sha256"],
                    "capture_file": f"context-{r['id']}.json",
                }
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
            "repeated_targeting": (row["harassment_volume"] > 0)
            if row.get("harassment_volume") is not None
            else None,
        },
        "checks": [
            {"element": c.label, "met": c.ok, "required": c.required, "detail": c.detail}
            for c in a.checks
        ],
    }
