"""Bounded, exact source excerpts for ID-only claim selection.

Candidates preserve source wording, including questions, speaker labels, opinions,
negation and unresolved pronouns. They are evidence excerpts, not paraphrases or
proof that the source is correct. Incomplete or oversized units are omitted rather
than clipped to fit a passage or generation budget.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .records import ChunkRecord

MAX_CANDIDATES = 24
MAX_INPUT_CHARS = 12_000
MAX_EXCERPT_CHARS = 800
MIN_EXCERPT_CHARS = 10
MAX_SUPPLIED_CHUNKS = 8

_SENTENCE_END = re.compile(r"[.!?…。！？][\"'”’“‘»›」』)\]}]*$")
_QUESTION_END = re.compile(r"[?？][\"'”’“‘»›」』)\]}]*$")
_URL_ONLY = re.compile(r"(?:https?://\S+\s*)+", re.IGNORECASE)
_SPEAKER = re.compile(r"^(?P<label>[^\W\d_][^:.!?/\n]{0,70}):(?:\s|$)", re.UNICODE)
_INTERVIEWERS = frozenset(
    {"tagesthemen", "tagesschau", "interviewer", "interviewerin", "frage", "question", "q"}
)


@dataclass(frozen=True, slots=True)
class SourceExcerpt:
    candidate_id: str
    chunk_id: int
    text: str
    start_char: int
    end_char: int

    def prompt_record(self) -> dict[str, str | int]:
        return {
            "candidate_id": self.candidate_id,
            "chunk_id": self.chunk_id,
            "text": self.text,
            "start_char": self.start_char,
            "end_char": self.end_char,
        }


@dataclass(frozen=True, slots=True)
class _Chunk:
    id: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _Unit:
    start: int
    end: int
    text: str
    speaker: str | None
    question: bool
    usable: bool
    complete: bool


def _usable_text(text: str) -> bool:
    first_letter = next((character for character in text if character.isalpha()), "")
    uncased_letters = any(
        character.isalpha() and not character.islower() and not character.isupper()
        for character in text
    )
    return (
        MIN_EXCERPT_CHARS <= len(text) <= MAX_EXCERPT_CHARS
        and bool(_SENTENCE_END.search(text))
        and not _URL_ONLY.fullmatch(text)
        and not (text.startswith("#") and "\n" not in text and "\r" not in text)
        and (not text.isupper() or uncased_letters)
        and (len(re.findall(r"\w+", text, re.UNICODE)) >= 3 or uncased_letters)
        and (
            bool(_SPEAKER.match(text))
            or first_letter.isupper()
            or (bool(first_letter) and not first_letter.islower())
        )
    )


def _units(source: str) -> list[_Unit]:
    result = []
    for match in re.finditer(r"[^\r\n]+", source):
        raw = match.group()
        text = raw.strip()
        if not text:
            continue
        start = match.start() + len(raw) - len(raw.lstrip())
        end = start + len(text)
        speaker_match = _SPEAKER.match(text)
        speaker = speaker_match.group("label").strip() if speaker_match else None
        question = bool(_QUESTION_END.search(text)) or (
            speaker is not None and speaker.casefold() in _INTERVIEWERS
        )
        label_only = speaker_match is not None and not text[speaker_match.end() :].strip()
        result.append(
            _Unit(
                start,
                end,
                text,
                speaker,
                question,
                _usable_text(text) or label_only,
                bool(_SENTENCE_END.search(text)),
            )
        )
    return result


def _chunks(source: str, supplied: Sequence[ChunkRecord | Mapping[str, object]]) -> list[_Chunk]:
    result = []
    seen = set()
    for item in supplied[:MAX_SUPPLIED_CHUNKS]:
        if isinstance(item, ChunkRecord):
            chunk_id, start, end, text = item.id, item.start_char, item.end_char, item.text
        elif isinstance(item, Mapping):
            chunk_id, start, end, text = (
                item.get("id"),
                item.get("start_char"),
                item.get("end_char"),
                item.get("text"),
            )
        else:
            raise ValueError("Supplied passages must provide saved IDs, offsets, and text.")
        if (
            type(chunk_id) is not int
            or type(start) is not int
            or type(end) is not int
            or chunk_id <= 0
            or chunk_id in seen
            or not isinstance(text, str)
            or not 0 <= start < end <= len(source)
            or source[start:end] != text
        ):
            raise ValueError(
                "Supplied passage IDs, offsets, and text must match the saved article."
            )
        seen.add(chunk_id)
        result.append(_Chunk(chunk_id, start, end))
    return sorted(result, key=lambda chunk: (chunk.start, chunk.end, chunk.id))


def build_claim_excerpts(
    article: Mapping[str, object],
    chunks: Sequence[ChunkRecord | Mapping[str, object]],
    *,
    max_candidates: int = MAX_CANDIDATES,
    max_input_chars: int = MAX_INPUT_CHARS,
) -> list[SourceExcerpt]:
    """Build complete source-line excerpts contained in one of the first eight chunks.

    Missing source text and inconsistent passage identities/offsets raise ValueError;
    fragments alone cannot establish whether a source paragraph was clipped. Valid
    source text without complete eligible units produces an empty candidate list.
    Limits may be lowered, never increased past the hard request bounds. The input
    budget measures ``json.dumps([candidate.prompt_record(), ...], ensure_ascii=False)``.
    """
    if (
        type(max_candidates) is not int
        or not 0 <= max_candidates <= MAX_CANDIDATES
        or type(max_input_chars) is not int
        or not 0 <= max_input_chars <= MAX_INPUT_CHARS
    ):
        raise ValueError("Excerpt limits must be integers within the hard request bounds.")
    source = article.get("text")
    if not isinstance(source, str) or not source:
        raise ValueError("A nonempty saved article body is required to build source excerpts.")
    supplied = _chunks(source, chunks)
    if max_candidates == 0 or not supplied:
        return []
    candidates: list[SourceExcerpt] = []
    ranges: set[tuple[int, int]] = set()
    consumed = 2  # JSON list brackets; inter-record separators are counted below.
    context_start: int | None = None
    pending_start: int | None = None
    previous: _Unit | None = None
    for unit in _units(source):
        # An unfinished line may carry denial, attribution, uncertainty or another
        # essential qualifier. Preserve its entire raw range, across blank lines,
        # headings and speaker changes, until a complete eligible excerpt fits.
        if not unit.complete and not _URL_ONLY.fullmatch(unit.text):
            if pending_start is None:
                pending_start = unit.start
        if unit.question:
            if previous is None or not previous.question:
                context_start = pending_start if pending_start is not None else unit.start
            elif pending_start is not None:
                context_start = min(
                    context_start if context_start is not None else unit.start, pending_start
                )
            previous = unit
            continue
        if not unit.usable:
            if pending_start is None:
                context_start = None
                previous = unit
                continue
            first_letter = next((character for character in unit.text if character.isalpha()), "")
            if (
                not unit.complete
                or _URL_ONLY.fullmatch(unit.text)
                or unit.text.startswith("#")
                or not first_letter.islower()
            ):
                previous = unit
                continue
        if unit.speaker is not None:
            # A previous question remains evidence context; a new answer/voice resets
            # any previous speaker context instead of transferring their identity.
            if previous is None or not previous.question:
                context_start = unit.start
        start = min(
            value for value in (unit.start, context_start, pending_start) if value is not None
        )
        end = unit.end
        text = source[start:end]
        previous = unit
        if not _usable_text(text) or (start, end) in ranges:
            continue
        containing = next(
            (chunk for chunk in supplied if chunk.start <= start and end <= chunk.end), None
        )
        if containing is None:
            # Keep the complete context requirement; do not turn the surviving tail
            # of a speaker turn into an apparently self-contained assertion.
            continue
        candidate = SourceExcerpt(f"e{len(candidates) + 1:02}", containing.id, text, start, end)
        cost = len(json.dumps(candidate.prompt_record(), ensure_ascii=False)) + (
            2 if candidates else 0
        )
        if consumed + cost > max_input_chars:
            continue
        candidates.append(candidate)
        pending_start = None
        ranges.add((start, end))
        consumed += cost
        if len(candidates) >= max_candidates:
            break
    return candidates
