"""Bounded saved-source comparison within a suggested event."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from itertools import combinations, islice
from math import comb

from ..review.comparison import DifferenceCandidate, SourceCoverage, compare_sources
from ..review.contracts import EvidenceBundle, Record

_PAIR_LIMIT = 8
_CANDIDATE_LIMIT = 12


@dataclass(frozen=True)
class EventDifferences:
    candidates: tuple[DifferenceCandidate, ...]
    compared_pairs: int
    eligible_pairs: int
    partial_pairs: int
    source_coverage: tuple[tuple[str, SourceCoverage], ...]
    candidates_truncated: bool


def possible_event_differences(
    bundle: EvidenceBundle, record_ids: Collection[str]
) -> EventDifferences:
    """Reuse exact passage comparison over a bounded sample of distinct saved text."""
    members = set(record_ids)
    by_hash: dict[str, Record] = {}
    for record in sorted(bundle.records, key=lambda item: item.id):
        if record.id in members and record.text and record.text.strip():
            by_hash.setdefault(record.content_hash, record)

    eligible_pairs = comb(len(by_hash), 2)
    comparisons = []
    for left, right in islice(combinations(by_hash.values(), 2), _PAIR_LIMIT):
        # The existing comparator only needs the two exact records and their case ID.
        pair = EvidenceBundle(
            case=bundle.case, records=[left, right], fragments=[], runs=[], findings=[]
        )
        comparisons.append(compare_sources(pair, left.id, right.id))

    candidates = tuple(candidate for pair in comparisons for candidate in pair.candidates)
    coverage = {
        source.record_id: checked
        for pair in comparisons
        for source, checked in ((pair.left, pair.coverage.left), (pair.right, pair.coverage.right))
    }
    return EventDifferences(
        candidates=candidates[:_CANDIDATE_LIMIT],
        compared_pairs=len(comparisons),
        eligible_pairs=eligible_pairs,
        partial_pairs=sum(
            pair.coverage.left.truncated or pair.coverage.right.truncated for pair in comparisons
        ),
        source_coverage=tuple(sorted(coverage.items())),
        candidates_truncated=(
            len(candidates) > _CANDIDATE_LIMIT
            or any(pair.coverage.candidates_truncated for pair in comparisons)
        ),
    )
