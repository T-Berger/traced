"""Shared, bounded saved-source context for both local claim-review passes."""

from __future__ import annotations

import json

from .contracts import EvidenceFragment, EvidenceSnapshot

MAX_PROMPT_BYTES = 28_000

CLAIM_AND_SOURCE_RULES = (
    "Assess the claim exactly as written, preserving attribution, negation, quantities, dates, "
    "location and qualifiers such as reportedly, purportedly, may, and candidate. Do not "
    "silently strengthen a qualified claim. Distinguish evidence that someone reported an "
    "assertion from evidence that the underlying event occurred. Do not replace 'reportedly X' "
    "with 'X happened': these are different propositions. A dispute over X alone does not "
    "disprove the existence of the report. A contradiction must conflict "
    "with the same proposition including its qualifications; a scope clarification or a "
    "different interpretation is not automatically a contradiction. When supplied passages "
    "contain a material criticism and reply, address both with citations and explain what "
    "remains unresolved. Do not present one side as uncontested. "
    "Source IDs group passages from the same saved record, not independent confirmations. "
    "Titles, URLs and collected metadata identify sources but do not authenticate their claims "
    "or supply missing content. A retrieved page may be a summary, repost, abstract or access "
    "page rather than the complete work. Base substantive conclusions on the supplied text; "
    "do not give a derivative summary independent evidential weight. Prefer a supplied original "
    "over its repetition when the relationship is established by the supplied material. "
    "Do not infer source independence or reliability from different domains, record counts, "
    "or successful retrieval. Passage offsets and saved-text lengths describe excerpt coverage, "
    "not coverage of the full original publication. Publication and retrieval times do not "
    "establish event time or the present status of a historical dispute. "
)


def saved_passage_context(
    claim: str, snapshot: EvidenceSnapshot
) -> tuple[dict[str, EvidenceFragment], str]:
    """Keep passage aliases and exact text identical across both passes, without mutation."""
    aliases = {f"P{index + 1}": fragment for index, fragment in enumerate(snapshot.fragments)}
    referenced = {fragment.record_id for fragment in snapshot.fragments}
    source_ids = {
        record.id: f"S{index + 1}"
        for index, record in enumerate(snapshot.records)
        if record.id in referenced
    }
    data = json.dumps(
        {
            "claim": claim,
            "sources": [
                {
                    "source_id": source_ids[record.id],
                    "title": record.title,
                    "url": record.url,
                    "publisher": record.publisher,
                    "published_at": record.published_at,
                    "retrieved_at": record.retrieved_at,
                    "saved_text_chars": len(record.text),
                }
                for record in snapshot.records
                if record.id in referenced
            ],
            "passages": [
                {
                    "fragment_id": alias,
                    "source_id": source_ids[fragment.record_id],
                    "start": fragment.start,
                    "end": fragment.end,
                    "text": fragment.text,
                }
                for alias, fragment in aliases.items()
            ],
        },
        ensure_ascii=False,
    )
    if len(data.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError("Claim review prompt exceeds its bounded input size.")
    return aliases, data
