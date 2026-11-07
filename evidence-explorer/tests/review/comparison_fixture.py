"""Clearly synthetic source material for deterministic comparison regressions."""

import hashlib

from evidence_explorer.review.contracts import EvidenceBundle

LEFT_TEXT = (
    "Synthetic fixture — 🧪 exact source text.\n"
    "Harbor bridge carries 20 vehicles per hour on Monday.\n"
    "Harbor council did approve the bridge reopening on Monday.\n"
    "Harbor bridge spans 1 km across the river."
)
RIGHT_TEXT = (
    "Synthetic fixture — 🧪 exact source text.\n"
    "Harbor bridge carries 35 vehicles per hour on Monday.\n"
    "Harbor council did not approve the bridge reopening on Monday.\n"
    "Harbor bridge spans 1000 m across the river."
)


def make_comparison_bundle(texts=None, *, case_id="synthetic-comparison"):
    texts = (
        {
            "left": LEFT_TEXT,
            "right": RIGHT_TEXT,
            "equivalent": "Harbor bridge spans 1000 m across the river.",
        }
        if texts is None
        else texts
    )
    records = [
        {
            "id": record_id,
            "kind": "article" if text is not None else "image",
            "source_label": f"Synthetic source {record_id}",
            "text": text,
            "content_hash": hashlib.sha256(
                (text if text is not None else "synthetic media").encode()
            ).hexdigest(),
            "source_url": f"https://example.org/{record_id}",
            "artifact_ref": None if text is not None else "fixtures/synthetic.svg",
            "published_at": None,
            "acquired_at": None,
            "metadata": {"synthetic": True},
        }
        for record_id, text in texts.items()
    ]
    return EvidenceBundle.model_validate(
        {
            "case": {
                "id": case_id,
                "title": "Synthetic comparison test — invented bridge statements",
                "question": "Which saved statements differ?",
                "synthetic": True,
                "limitations": ["All sources and statements in this fixture are invented."],
            },
            "records": records,
            "fragments": [],
            "runs": [],
            "findings": [],
        }
    )
