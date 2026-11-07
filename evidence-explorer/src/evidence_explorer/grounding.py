"""Exact source quotation checks shared by generation and persisted analysis."""

from .claim_excerpts import SourceExcerpt
from .models import ClaimInput
from .records import ChunkRecord, SourcePassage


def validate_claims(
    claims: list[ClaimInput], chunks: list[ChunkRecord] | list[dict]
) -> list[ClaimInput]:
    """Every quote must exist in its declared passage, including legacy wire inputs."""
    passages = [
        SourcePassage.model_validate(
            chunk.model_dump() if isinstance(chunk, ChunkRecord) else chunk
        )
        for chunk in chunks
    ]
    by_id = {passage.id: passage.text for passage in passages}
    seen: set[tuple[str, int, str]] = set()
    validated: list[ClaimInput] = []
    for claim in claims:
        text = by_id.get(claim.chunk_id)
        if text is None:
            raise ValueError(f"Claim refers to unknown chunk {claim.chunk_id}.")
        if claim.quote not in text:
            raise ValueError("Claim quote does not occur verbatim in the supporting passage.")
        identity = (claim.claim_text, claim.chunk_id, claim.quote)
        if identity not in seen:
            validated.append(claim)
            seen.add(identity)
    return validated


def validate_extractive_claims(
    claims: list[ClaimInput],
    chunks: list[ChunkRecord] | list[dict],
    *,
    candidates: list[SourceExcerpt] | None = None,
) -> list[ClaimInput]:
    """The current extractor may quote evidence, never add an interpretation.

    Legacy/imported paraphrases retain the separate quotation-only validator.
    New extracted assertions cannot attach an inferred person, speaker, date or
    location, even when another genuine quote exists in the same passage.
    """
    if any(
        claim.claim_text != claim.quote
        or claim.subject
        or claim.attribution
        or claim.event_time is not None
        or claim.location is not None
        for claim in claims
    ):
        raise ValueError("Extracted claims must preserve source wording and unknown attribution.")
    if candidates is not None:
        allowed = {(candidate.chunk_id, candidate.text) for candidate in candidates}
        if any((claim.chunk_id, claim.quote) not in allowed for claim in claims):
            raise ValueError("Extracted claims must preserve a complete eligible source excerpt.")
    return validate_claims(claims, chunks)
