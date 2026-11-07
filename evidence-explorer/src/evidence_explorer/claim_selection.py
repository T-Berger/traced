"""A model may select source excerpts, but cannot rewrite their assertions."""

from pydantic import BaseModel, ConfigDict, Field

from .claim_excerpts import SourceExcerpt
from .models import ClaimInput

CLAIMS_POLICY_VERSION = "claims-v2-source-excerpts"


class ExcerptSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1, max_length=64, strict=True)


class ClaimSelectionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claims: list[ExcerptSelection] = Field(max_length=3)


def selected_claims(
    selections: list[ExcerptSelection], candidates: list[SourceExcerpt]
) -> list[ClaimInput]:
    """Derive every assertion and its unknown metadata from retained source text.

    A speaker's name elsewhere in an article is not evidence of a pronoun's
    referent. Keeping the complete excerpt unchanged avoids resolving it at all.
    Unknown IDs fail closed, including IDs that exist only outside the request.
    """
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    accepted = []
    seen = set()
    for selection in selections:
        excerpt = by_id.get(selection.candidate_id)
        if excerpt is None:
            raise ValueError(f"Unknown source excerpt {selection.candidate_id!r}.")
        if excerpt.candidate_id in seen:
            continue
        accepted.append(
            ClaimInput(claim_text=excerpt.text, quote=excerpt.text, chunk_id=excerpt.chunk_id)
        )
        seen.add(excerpt.candidate_id)
    return accepted
