"""Read-only, evidence-linked monthly category review projections.

A confirmed category describes a reviewer's classification of a saved post; it is
not a legal finding, a platform decision, or an automated severity verdict.
"""

from collections import defaultdict
from datetime import UTC, datetime
from hashlib import sha256

CATEGORIES = ("disinformation", "racism", "harassment", "hate_speech")
COVERAGE = {"complete", "partial", "unknown"}


def _month(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC).strftime("%Y-%m")
    except ValueError:
        return None


def _reference(value, records, *, fictional, post_id):
    """Retain a notice/response only as an exact, separately saved observation."""
    if not isinstance(value, dict):
        return {"status": "unknown", "reason": "No linked saved observation"}
    source = records.get(value.get("record_id"))
    quote = value.get("quote")
    if (
        not source
        or value.get("record_id") == post_id
        or not isinstance(quote, str)
        or not quote
        or quote not in source.get("text", "")
    ):
        return {"status": "unknown", "reason": "Referenced observation or exact quote unavailable"}
    if not fictional:
        return {
            **value,
            "status": "unverified",
            "reason": "Linked text alone does not establish notice or platform response",
        }
    return {**value, "status": "documented"}


def build_monthly_matrix(records, annotations, coverage, *, fictional=False):
    """Project supplied saved records without writing or transferring reviews.

    Only the explicitly fictional example can use supplied coverage or confirmed
    categories: ordinary dictionaries do not establish actual coverage or human
    review authority. Each row counts one distinct saved post ID. Unknown,
    partial, and missing coverage never imply zero complete observations.
    """
    by_id = {}
    for record in records:
        key = record.get("id")
        if not isinstance(key, str) or not key or key in by_id:
            raise ValueError("Each saved record must have a unique nonempty ID")
        if not isinstance(record.get("text"), str):
            raise ValueError("Every saved record needs its saved text")
        by_id[key] = record
    grouped = defaultdict(list)
    invalid = []
    for annotation in annotations:
        record = by_id.get(annotation.get("record_id"))
        quote = annotation.get("quote")
        reason = None
        if record is None or record.get("kind") != "post":
            reason = "Saved post unavailable"
        elif annotation.get("category") not in CATEGORIES:
            reason = "Unknown category"
        elif annotation.get("origin") not in {"human", "model"}:
            reason = "Annotation origin is unavailable"
        elif annotation.get("origin") == "model" and not annotation.get("model"):
            reason = "Model provenance is unavailable"
        elif not isinstance(quote, str) or not quote or quote not in record["text"]:
            reason = "Exact quotation unavailable in saved post"
        elif (
            annotation.get("record_content_hash")
            != sha256(record["text"].encode("utf-8")).hexdigest()
        ):
            reason = "Annotation is not bound to this saved text version"
        elif (
            type(annotation.get("start")) is not int
            or type(annotation.get("end")) is not int
            or annotation["start"] < 0
            or annotation["end"] <= annotation["start"]
            or record["text"][annotation["start"] : annotation["end"]] != quote
        ):
            reason = "Exact fragment offsets do not match the saved post"
        elif annotation.get("status") == "confirmed" and (
            annotation.get("origin") != "human" or not annotation.get("reviewer")
        ):
            reason = "Confirmed category requires an attributed human review"
        elif annotation.get("status") not in {"confirmed", "proposed", "rejected"}:
            reason = "Unknown review status"
        elif not fictional and annotation.get("status") in {"confirmed", "rejected"}:
            reason = "Untrusted review claim: no linked saved human decision"
        if reason:
            invalid.append({"annotation": dict(annotation), "reason": reason})
        else:
            grouped[record["id"]].append(dict(annotation))

    rows = []
    undated = []
    excluded = []
    month_rows = defaultdict(list)
    for record in records:
        if record.get("kind") != "post":
            excluded.append({"record_id": record["id"], "kind": record.get("kind")})
            continue
        labels = grouped[record["id"]]
        row = {
            "record_id": record["id"],
            "published_at": record.get("published_at"),
            "text": record["text"],
            "categories": [a for a in labels if a["status"] == "confirmed"],
            "proposals": [a for a in labels if a["status"] == "proposed"],
            "rejected": [a for a in labels if a["status"] == "rejected"],
            "notice": _reference(
                record.get("notice"), by_id, fictional=fictional, post_id=record["id"]
            ),
            "response": _reference(
                record.get("response"), by_id, fictional=fictional, post_id=record["id"]
            ),
            "swarm": record.get("swarm"),
        }
        rows.append(row)
        month = _month(record.get("published_at"))
        if month:
            month_rows[month].append(row)
        else:
            undated.append(record["id"])

    months = []
    for month in sorted(set(month_rows) | set(coverage)):
        status = coverage.get(month, "unknown") if fictional else "unknown"
        if status not in COVERAGE:
            status = "unknown"
        categories = {}
        for category in CATEGORIES:
            confirmed_ids = sorted(
                {
                    r["record_id"]
                    for r in month_rows[month]
                    if any(a["category"] == category for a in r["categories"])
                }
            )
            proposed_ids = sorted(
                {
                    r["record_id"]
                    for r in month_rows[month]
                    if any(a["category"] == category for a in r["proposals"])
                }
            )
            categories[category] = {
                "confirmed": len(confirmed_ids) if status == "complete" else None,
                "proposed": len(proposed_ids) if status == "complete" else None,
                "observed_confirmed": len(confirmed_ids),
                "observed_proposed": len(proposed_ids),
                "record_ids": confirmed_ids,
                "proposed_record_ids": proposed_ids,
            }
        months.append({"month": month, "coverage": status, "categories": categories})
    return {
        "months": months,
        "rows": rows,
        "undated_record_ids": undated,
        "excluded_records": excluded,
        "invalid_annotations": invalid,
        "count_unit": "distinct saved post IDs",
        "time_basis": "published_at in UTC",
        "limitation": (
            "Category review is not a violation, platform decision, or proof of enforcement."
        ),
    }


def fictional_demo():
    """Return a small, explicitly fictional example; all names and events are invented."""
    records = [
        {
            "id": "fiction:post:1",
            "kind": "post",
            "published_at": "2026-01-10T10:00:00Z",
            "text": "Fictional example: an invented rumor claims the election was canceled.",
            "notice": {
                "record_id": "fiction:notice:1",
                "quote": "A fictional report was submitted on January 11.",
            },
            "response": {
                "record_id": "fiction:observation:1",
                "quote": "The fictional post was still visible on January 13.",
            },
        },
        {
            "id": "fiction:post:2",
            "kind": "post",
            "published_at": "2026-02-12T11:00:00Z",
            "text": "Fictional example: a character repeatedly threatens another character.",
        },
        {
            "id": "fiction:post:3",
            "kind": "post",
            "published_at": "2026-03-03T09:00:00Z",
            "text": "Fictional example: a statement attacks an invented community.",
        },
        {
            "id": "fiction:post:4",
            "kind": "post",
            "published_at": None,
            "text": "Fictional example: an undated comment requiring review.",
        },
        {
            "id": "fiction:notice:1",
            "kind": "notice",
            "text": "A fictional report was submitted on January 11.",
        },
        {
            "id": "fiction:observation:1",
            "kind": "observation",
            "text": "The fictional post was still visible on January 13.",
        },
    ]
    annotations = [
        {
            "record_id": "fiction:post:1",
            "category": "disinformation",
            "origin": "human",
            "status": "confirmed",
            "reviewer": "Fictional reviewer",
            "quote": "an invented rumor claims the election was canceled",
            "method": "Fictional exercise",
        },
        {
            "record_id": "fiction:post:2",
            "category": "harassment",
            "origin": "human",
            "status": "confirmed",
            "reviewer": "Fictional reviewer",
            "quote": "a character repeatedly threatens another character",
            "method": "Fictional exercise",
        },
        {
            "record_id": "fiction:post:3",
            "category": "hate_speech",
            "origin": "model",
            "status": "proposed",
            "quote": "a statement attacks an invented community",
            "method": "Fictional exercise",
            "model": "fictional example",
        },
    ]
    texts = {record["id"]: record["text"] for record in records}
    for annotation in annotations:
        annotation["record_content_hash"] = sha256(
            texts[annotation["record_id"]].encode("utf-8")
        ).hexdigest()
        annotation["start"] = texts[annotation["record_id"]].index(annotation["quote"])
        annotation["end"] = annotation["start"] + len(annotation["quote"])
    return {
        "title": "Fictional compliance review example",
        "fictional": True,
        "coverage": {
            "2026-01": "complete",
            "2026-02": "complete",
            "2026-03": "partial",
            "2026-04": "unknown",
        },
        "records": records,
        "annotations": annotations,
    }
