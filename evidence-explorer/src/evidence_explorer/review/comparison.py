"""Bounded, read-only comparison of two immutable saved text sources.

Rules identify wording differences worth inspecting, never factual contradictions.
Matching may ignore letter case; displayed text and offsets always remain exact.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .contracts import EvidenceBundle, Record

METHOD_VERSION = "saved-source-comparison-v1"
MAX_SOURCE_CHARS = 40_000
MAX_SENTENCES = 200
MAX_CANDIDATES = 20
MIN_WORD_ANCHORS = 4
_NEGATIONS = {"no", "not", "never"}
_SENTENCE = re.compile(r"\S.*?(?:[.!?](?=\s|$)|\n+|$)", re.DOTALL)
_TOKEN = re.compile(r"[^\W\d_][^\W_]*(?:['’][^\W_]+)*|\d+(?:\.\d+)?|[^\w\s]")
_NUMBER = re.compile(r"\d+(?:\.\d+)?\Z")


class ComparisonModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceMetadata(ComparisonModel):
    title: str | None = None
    publisher: str | None = None
    platform: str | None = None
    channel_name: str | None = None
    author: str | None = None
    requested_url: str | None = None
    final_url: str | None = None
    feed_url: str | None = None
    retrieved_at: str | None = None
    publication_source: str | None = None
    content_source: str | None = None
    synthetic: bool | None = None


class SourceView(ComparisonModel):
    record_id: str
    content_hash: str
    source_label: str
    source_url: str | None
    published_at: str | None
    acquired_at: str | None
    text: str
    metadata: SourceMetadata


class SourceCoverage(ComparisonModel):
    total_chars: int
    displayed_chars: int
    examined_chars: int
    total_sentences: int | None
    examined_sentences: int
    truncated: bool
    character_limit: int = MAX_SOURCE_CHARS
    sentence_limit: int = MAX_SENTENCES


class ComparisonCoverage(ComparisonModel):
    left: SourceCoverage
    right: SourceCoverage
    candidate_limit: int = MAX_CANDIDATES
    candidates_truncated: bool


class Highlight(ComparisonModel):
    start: int
    end: int
    kind: Literal["match", "difference"]


class ComparedPassage(ComparisonModel):
    record_id: str
    start: int
    end: int
    quote: str
    highlights: tuple[Highlight, ...]


class DifferenceCandidate(ComparisonModel):
    id: str
    kind: Literal["numeric_difference", "negation_difference"]
    label: str
    basis: str
    left: ComparedPassage
    right: ComparedPassage


class SourceComparison(ComparisonModel):
    pair_id: str
    case_id: str
    method_version: str = METHOD_VERSION
    left: SourceView
    right: SourceView
    candidates: tuple[DifferenceCandidate, ...]
    coverage: ComparisonCoverage
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class _Token:
    start: int
    end: int
    normalized: str
    kind: Literal["word", "number", "punctuation"]


@dataclass(frozen=True)
class _Sentence:
    record_id: str
    start: int
    end: int
    quote: str
    tokens: tuple[_Token, ...]


def _identity(prefix: str, payload: object) -> str:
    raw = json.dumps([METHOD_VERSION, payload], ensure_ascii=False, separators=(",", ":"))
    return prefix + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _tokens(text: str) -> tuple[_Token, ...]:
    return tuple(
        _Token(
            match.start(),
            match.end(),
            match.group().casefold(),
            "number"
            if _NUMBER.fullmatch(match.group())
            else "word"
            if match.group()[0].isalpha()
            else "punctuation",
        )
        for match in _TOKEN.finditer(text)
    )


def _source(record: Record) -> tuple[SourceView, SourceCoverage, list[_Sentence]]:
    text = record.text
    if text is None or not text.strip():
        raise ValueError(
            "Choose two records with saved visible text; media alone cannot be compared."
        )
    prefix = text[:MAX_SOURCE_CHARS]
    char_limited = len(prefix) < len(text)
    ranges = []
    for match in _SENTENCE.finditer(prefix):
        start, end = match.start(), match.end()
        while end > start and prefix[end - 1].isspace():
            end -= 1
        # A cut-off tail is context, not a complete sentence eligible for flags.
        if char_limited and match.end() == len(prefix):
            boundary = prefix[match.end() - 1].isspace() and "\n" in match.group()
            boundary |= prefix[end - 1] in ".!?" and text[end : end + 1].isspace()
            if not boundary:
                continue
        if end > start:
            ranges.append((start, end))
    chosen = ranges[:MAX_SENTENCES]
    sentence_limited = len(ranges) > MAX_SENTENCES
    displayed_chars = chosen[-1][1] if sentence_limited else len(prefix)
    truncated = char_limited or sentence_limited
    examined_chars = chosen[-1][1] if chosen else 0
    if not truncated:
        examined_chars = len(text)
    metadata = {
        key: value if isinstance(value, str) and value.strip() else None
        for key in SourceMetadata.model_fields
        if key != "synthetic"
        for value in [record.metadata.get(key)]
    }
    synthetic = record.metadata.get("synthetic")
    metadata["synthetic"] = synthetic if type(synthetic) is bool else None
    view = SourceView(
        record_id=record.id,
        content_hash=record.content_hash,
        source_label=record.source_label,
        source_url=record.source_url,
        published_at=record.published_at,
        acquired_at=record.acquired_at,
        text=text[:displayed_chars],
        metadata=SourceMetadata.model_validate(metadata),
    )
    coverage = SourceCoverage(
        total_chars=len(text),
        displayed_chars=displayed_chars,
        examined_chars=examined_chars,
        total_sentences=None if char_limited else len(ranges),
        examined_sentences=len(chosen),
        truncated=truncated,
    )
    sentences = [
        _Sentence(record.id, start, end, text[start:end], _tokens(text[start:end]))
        for start, end in chosen
    ]
    return view, coverage, sentences


def _signature(sentence: _Sentence, kind: str) -> tuple[str, ...] | None:
    anchors = {
        token.normalized
        for token in sentence.tokens
        if token.kind == "word" and token.normalized not in _NEGATIONS
    }
    if len(anchors) < MIN_WORD_ANCHORS:
        return None
    if kind == "numeric_difference":
        if not any(token.kind == "number" for token in sentence.tokens):
            return None
        return tuple("#" if t.kind == "number" else t.normalized for t in sentence.tokens)
    return tuple(t.normalized for t in sentence.tokens if t.normalized not in _NEGATIONS)


def _differences(left: _Sentence, right: _Sentence, kind: str) -> tuple[set[int], set[int]] | None:
    if kind == "numeric_difference":
        changed = {
            index
            for index, (a, b) in enumerate(zip(left.tokens, right.tokens, strict=True))
            if a.kind == b.kind == "number" and Decimal(a.normalized) != Decimal(b.normalized)
        }
        return (changed, changed) if changed else None
    left_neg = {i for i, t in enumerate(left.tokens) if t.normalized in _NEGATIONS}
    right_neg = {i for i, t in enumerate(right.tokens) if t.normalized in _NEGATIONS}
    if sorted((len(left_neg), len(right_neg))) != [0, 1]:
        return None
    return left_neg, right_neg


def _passage(sentence: _Sentence, differences: set[int]) -> ComparedPassage:
    spans: list[Highlight] = []
    for index, token in enumerate(sentence.tokens):
        kind = "difference" if index in differences else "match"
        start, end = sentence.start + token.start, sentence.start + token.end
        if spans and spans[-1].kind == kind:
            previous = spans.pop()
            start = previous.start
        spans.append(Highlight(start=start, end=end, kind=kind))
    return ComparedPassage(
        record_id=sentence.record_id,
        start=sentence.start,
        end=sentence.end,
        quote=sentence.quote,
        highlights=tuple(spans),
    )


def _candidates(first: list[_Sentence], second: list[_Sentence]) -> list[DifferenceCandidate]:
    candidates = []
    for kind in ("numeric_difference", "negation_difference"):
        by_context: dict[tuple[str, ...], list[_Sentence]] = {}
        for sentence in second:
            signature = _signature(sentence, kind)
            if signature is not None:
                by_context.setdefault(signature, []).append(sentence)
        for left in first:
            for right in by_context.get(_signature(left, kind), []):
                changed = _differences(left, right, kind)
                if changed is None:
                    continue
                left_passage, right_passage = (
                    _passage(left, changed[0]),
                    _passage(right, changed[1]),
                )
                candidates.append(
                    DifferenceCandidate(
                        id=_identity(
                            "comparison:candidate:",
                            [
                                kind,
                                [left.record_id, left.start, left.end, left.quote],
                                [right.record_id, right.start, right.end, right.quote],
                            ],
                        ),
                        kind=kind,
                        label="Possible numeric difference"
                        if kind == "numeric_difference"
                        else "Possible negation difference",
                        basis=(
                            "Numbers differ in otherwise matching wording and units. They may "
                            "describe different dates, counts, or contexts; review both passages."
                            if kind == "numeric_difference"
                            else "One passage adds or removes a negation in otherwise matching "
                            "wording. Review the meaning and context of both passages."
                        ),
                        left=left_passage,
                        right=right_passage,
                    )
                )
                if len(candidates) > MAX_CANDIDATES:
                    return candidates
    return candidates


def compare_sources(bundle: EvidenceBundle, left_id: str, right_id: str) -> SourceComparison:
    """Compare one selected pair without inference, network calls, or storage writes."""
    bundle = EvidenceBundle.model_validate(bundle)
    if left_id == right_id:
        raise ValueError("Choose two different saved sources.")
    records = {record.id: record for record in bundle.records}
    if left_id not in records or right_id not in records:
        raise ValueError("A selected source is not in this saved investigation.")
    left, left_coverage, left_sentences = _source(records[left_id])
    right, right_coverage, right_sentences = _source(records[right_id])
    # Canonical processing order keeps IDs and bounded candidate selection stable on swap.
    reverse = left_id > right_id
    candidates = _candidates(
        right_sentences if reverse else left_sentences,
        left_sentences if reverse else right_sentences,
    )
    visible = candidates[:MAX_CANDIDATES]
    if reverse:
        visible = [
            item.model_copy(update={"left": item.right, "right": item.left}) for item in visible
        ]
    return SourceComparison(
        pair_id=_identity(
            "comparison:pair:",
            [
                bundle.case.id,
                sorted([[left_id, left.content_hash], [right_id, right.content_hash]]),
            ],
        ),
        case_id=bundle.case.id,
        left=left,
        right=right,
        candidates=tuple(visible),
        coverage=ComparisonCoverage(
            left=left_coverage,
            right=right_coverage,
            candidates_truncated=len(candidates) > MAX_CANDIDATES,
        ),
        limitations=(
            "Candidates are wording differences for human review, not established contradictions "
            "or assessments of source truth.",
            "Numeric changes may be dates, quantities, or other context changes. Publication "
            "and acquisition timestamps are not treated as event times.",
            "Only tightly matching sentence wording is compared, ignoring letter case. "
            "Negation detection covers the English words no, not, and never.",
            "No flagged difference does not establish agreement. Paraphrases, different contexts, "
            "and evidence outside the displayed coverage may contain unflagged differences.",
            "Matching text does not establish source independence, common authorship, "
            "or coordination. Differing units are not converted or treated as equivalent.",
        ),
    )
