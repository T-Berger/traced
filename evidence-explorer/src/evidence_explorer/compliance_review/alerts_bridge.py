"""Explicit, read-only bridge from Alerts snapshots to saved-record proposals.

The analytics database and the Evidence Trail workspace are independent stores.
The caller must explicitly select a mapping; the bridge never matches names or
searches for corresponding posts. No result is a saved review or decision.
"""

from __future__ import annotations

from hashlib import sha256

from ..alerts.analysis import violations
from ..alerts.repository import CaseData
from ..alerts.settings import AlertSettings
from ..review.comparison_ui import safe_source_url
from ..review.contracts import EvidenceBundle


def project_alert_snapshot(
    data: CaseData,
    bundle: EvidenceBundle | dict,
    mapping: dict[str, str],
    settings: AlertSettings,
) -> dict:
    """Prepare unreviewed annotations for explicit analytics-to-record mappings.

    ``data`` must come from ``AnalyticsRepository.case_snapshot`` for exactly
    the mapped analytics IDs. All IDs, text versions, safe source URLs, stored
    capture bytes, and declared linked contexts are checked. Matching URLs and
    text are not proof of collection authenticity or policy violation. Source
    context is preserved, not promoted to a shared EvidenceBundle reference.
    The operation is pure and does not store or approve anything.
    """
    bundle = EvidenceBundle.model_validate(
        bundle.model_dump(mode="json") if isinstance(bundle, EvidenceBundle) else bundle
    )
    if not mapping or any(
        not isinstance(k, str) or not k or not isinstance(v, str) or not v
        for k, v in mapping.items()
    ):
        raise ValueError("Explicit analytics item to saved-record mappings are required.")
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("Each selected analytics item requires a distinct saved record.")
    if set(data.provenance) != set(mapping):
        raise ValueError("Selected captures do not match the explicit mapping.")
    case_id = data.case.get("id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("The analytics case identity is unavailable.")
    rows = {row.get("id"): row for row in data.labels}
    if len(rows) != len(data.labels):
        raise ValueError("Multiple analytics labels have the same record ID.")
    records = {record.id: record for record in bundle.records}
    annotations = []
    mappings = []
    for item_id, record_id in mapping.items():
        row, record = rows.get(item_id), records.get(record_id)
        if not row or row.get("case_id") != case_id or record is None:
            raise ValueError("The selected analytics item or saved record is unavailable.")
        raw = data.provenance[item_id].get("raw")
        capture_hash = row.get("capture_sha256")
        if (
            not isinstance(raw, str)
            or not capture_hash
            or sha256(raw.encode()).hexdigest() != capture_hash
        ):
            raise ValueError("The selected analytics capture is missing or has changed.")
        text = row.get("text")
        source_url = safe_source_url(row.get("url"))
        if (
            not isinstance(text, str)
            or not text.strip()
            or text != record.text
            or not source_url
            or source_url != safe_source_url(record.source_url)
        ):
            raise ValueError(
                "Saved record text and source URL must match the selected analytics item."
            )
        model, prompt = row.get("model"), row.get("prompt_version")
        if (
            not isinstance(model, str)
            or not model.strip()
            or not isinstance(prompt, str)
            or not prompt.strip()
        ):
            raise ValueError("The candidate model and prompt version must be supplied.")
        references = []
        for ref_id in row.get("reference_ids") or []:
            ref = data.references.get(ref_id)
            raw_ref = ref.get("raw") if ref else None
            if (
                not ref
                or ref.get("id") != ref_id
                or ref.get("case_id") != case_id
                or not isinstance(raw_ref, str)
                or not ref.get("capture_sha256")
                or sha256(raw_ref.encode()).hexdigest() != ref["capture_sha256"]
            ):
                raise ValueError("A declared analytics context record is missing or has changed.")
            references.append({"id": ref_id, "capture_sha256": ref["capture_sha256"]})
        for category in violations(row, settings):
            annotations.append(
                {
                    "record_id": record.id,
                    "record_content_hash": record.content_hash,
                    "category": category,
                    "origin": "model",
                    "status": "proposed",
                    "quote": text,
                    "start": 0,
                    "end": len(text),
                    "model": model,
                    "method": f"alerts:{prompt}",
                    "score": row[category],
                    "analytics_case_id": case_id,
                    "analytics_item_id": item_id,
                    "capture_sha256": capture_hash,
                    "context_records": references,
                    "rationale": row.get("rationale"),
                    "interpretation": (
                        "Unreviewed classifier proposal; the saved text is context, "
                        "not proof that the category applies."
                    ),
                }
            )
        mappings.append({"analytics_item_id": item_id, "record_id": record.id})
    return {
        "case_id": bundle.case.id,
        "analytics_case_id": case_id,
        "mappings": mappings,
        "annotations": annotations,
        "limitations": [
            "The source and extraction require independent review; export hashes "
            "do not prove authenticity.",
            "Analytics labels are model proposals, not a review decision or violation.",
            "No coverage, platform response, account identity, or independent "
            "corroboration is inferred.",
        ],
    }
