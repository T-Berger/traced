"""Read-only suggestions for events in the saved dashboard records."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from ..review.analysis import source_groups_for
from ..review.contracts import EvidenceBundle
from ..review.models import SourceGroup
from .contracts import DashboardProjection

MATCHING_RULE = (
    "Suggestions use overlapping saved wording and publication dates within seven days. "
    "Matching compares a bounded set of candidates using the first 8,000 characters of each "
    "record. When both records give clear multiword names, at least one name must match. "
    "Exact text matches and documented reposts can link additional records, including undated "
    "records, to a single matching event. Saved claim relationships remain separate."
)
_EVENT_WINDOW_DAYS = 7
_MATCH_TEXT_LIMIT = 8_000
_MIN_SHARED_TERMS = 3
_INDEX_TERMS = 16
_RECENT_SEEDS = 32
_WORDS = re.compile(r"[^\W\d_]+")
_NAMES = re.compile(r"\b(?:[A-ZÀ-ÖØ-Þ][^\W\d_]+[ \t]+)+[A-ZÀ-ÖØ-Þ][^\W\d_]+")
_URL = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_COMMON_WORDS = frozenset(
    """
    about after again also and are before been being between both but can could did does doing
    down during each for from had has have here how into its just like may more most must now
    off once one only other our out over said says she should since some than that the their
    them then there these they this those through too under until upon was were what when
    where which while who will with would you your follow latest update updates news report
    reports posted read link monday tuesday wednesday thursday friday saturday sunday
    aber alle als auch auf aus bei bis das dass dem den der des die doch durch eine einem
    einen einer eines fuer für gegen hat hier ihre ihren im ist kann mit nach nicht noch nur
    oder ohne seit sind über und vom von vor war waren was weil werden wie wird wurde zum zur
    """.split()
)


@dataclass(frozen=True)
class EventPoint:
    label: str
    record_ids: tuple[str, ...]


@dataclass(frozen=True)
class EventClaim:
    statement: str
    supports: tuple[str, ...]
    contradicts: tuple[str, ...]
    finding_id: str = ""
    run_id: str = ""


@dataclass(frozen=True)
class EventCluster:
    id: str
    title: str
    matching_terms: tuple[str, ...]
    record_ids: tuple[str, ...]
    points: tuple[EventPoint, ...]
    repeat_groups: tuple[SourceGroup, ...]
    claims: tuple[EventClaim, ...]


@dataclass(frozen=True)
class EventProjection:
    events: tuple[EventCluster, ...]
    unclustered_record_ids: tuple[str, ...]


@dataclass
class _Candidate:
    seed_words: tuple[str, ...]
    seed_terms: frozenset[str]
    common_terms: frozenset[str]
    names: frozenset[str]
    record_ids: list[str]
    first: date
    last: date


def _post_terms(text: str) -> tuple[tuple[str, ...], frozenset[str]]:
    sample = _URL.sub(" ", unicodedata.normalize("NFKC", text[:_MATCH_TEXT_LIMIT]))
    words = tuple(
        dict.fromkeys(
            word
            for word in _WORDS.findall(sample.casefold())
            if len(word) >= 3 and word not in _COMMON_WORDS
        )
    )
    names = set()
    for match in _NAMES.finditer(sample):
        parts = tuple(word for word in match[0].casefold().split() if word not in _COMMON_WORDS)
        names.update(f"{left} {right}" for left, right in pairwise(parts))
    return words, frozenset(names)


def _within_window(first: date, last: date, day: date) -> bool:
    return (max(last, day) - min(first, day)).days <= _EVENT_WINDOW_DAYS


def _match(
    candidate: _Candidate, terms: frozenset[str], names: frozenset[str], day: date
) -> tuple[int, int] | None:
    if (
        not _within_window(candidate.first, candidate.last, day)
        or candidate.names
        and names
        and candidate.names.isdisjoint(names)
    ):
        return None
    common = len(candidate.common_terms & terms)
    if common < _MIN_SHARED_TERMS:
        return None
    overlap = len(candidate.seed_terms & terms)
    smaller = min(len(candidate.seed_terms), len(terms))
    union = len(candidate.seed_terms) + len(terms) - overlap
    if overlap * 2 < smaller or overlap * 100 < union * 35:
        return None
    return common, overlap


def _wording_candidates(
    post_terms: dict[str, tuple[tuple[str, ...], frozenset[str]]],
    days: dict[str, date | None],
) -> list[_Candidate]:
    dated = sorted(
        (day, rid)
        for rid, day in days.items()
        if day is not None and len(post_terms[rid][0]) >= _MIN_SHARED_TERMS
    )
    frequency = Counter(word for _, rid in dated for word in post_terms[rid][0])
    by_term: defaultdict[str, deque[int]] = defaultdict(lambda: deque(maxlen=_RECENT_SEEDS))
    candidates: list[_Candidate] = []
    for day, rid in dated:
        words, names = post_terms[rid]
        terms = frozenset(words)
        anchors = sorted(terms, key=lambda term: (frequency[term], term))[:_INDEX_TERMS]
        possible = {index for term in anchors for index in by_term.get(term, ())}
        matches = [
            (*score, -index)
            for index in possible
            if (score := _match(candidates[index], terms, names, day)) is not None
        ]
        if matches:
            candidate = candidates[-max(matches)[2]]
            candidate.common_terms &= terms
            if names:
                candidate.names = candidate.names & names if candidate.names else names
            candidate.record_ids.append(rid)
            candidate.last = day
            continue
        for term in terms:
            by_term[term].append(len(candidates))
        candidates.append(_Candidate(words, terms, terms, names, [rid], day, day))
    return candidates


def _attach_repeats(
    candidates: list[_Candidate],
    groups: tuple[SourceGroup, ...],
    days: dict[str, date | None],
) -> None:
    """Use saved repeat links without allowing a repeat to join two event candidates."""
    seed_membership = {
        rid: index for index, event in enumerate(candidates) for rid in event.record_ids
    }
    choices: defaultdict[str, set[int]] = defaultdict(set)
    for group in groups:
        matches = {seed_membership[rid] for rid in group.record_ids if rid in seed_membership}
        established = {index for index in matches if len(candidates[index].record_ids) > 1}
        eligible = established or matches
        if len(eligible) != 1:
            continue
        (target,) = eligible
        event = candidates[target]
        for rid in group.record_ids:
            if rid not in days or seed_membership.get(rid) == target:
                continue
            previous = seed_membership.get(rid)
            if previous is not None and len(candidates[previous].record_ids) > 1:
                continue
            day = days[rid]
            if day is None or _within_window(event.first, event.last, day):
                choices[rid].add(target)

    destinations = {
        rid: next(iter(options)) for rid, options in choices.items() if len(options) == 1
    }
    anchored = set(destinations.values())
    for rid, target in sorted(destinations.items()):
        previous = seed_membership.get(rid)
        if previous in anchored:
            continue
        event = candidates[target]
        day = days[rid]
        if day is not None:
            if not _within_window(event.first, event.last, day):
                continue
            event.first = min(event.first, day)
            event.last = max(event.last, day)
        if previous is not None:
            candidates[previous].record_ids.remove(rid)
        event.record_ids.append(rid)


def _repeat_groups(bundle: EvidenceBundle, scoped_ids: set[str]) -> tuple[SourceGroup, ...]:
    # Text equality is safe to intersect. Repost membership uses direct saved links
    # so that excluded or out-of-event posts cannot connect two visible records.
    text_groups = source_groups_for(bundle.model_copy(update={"links": []}))
    reposts = tuple(
        SourceGroup(
            id=link.id,
            kind="documented_repost",
            label="Documented repost link",
            record_ids=(link.source.id, link.target.id),
            origin_record_id=None,
            independence="unknown",
            basis=link.basis,
        )
        for link in sorted(bundle.links, key=lambda link: link.id)
        if link.relation == "documented_repost"
        and link.source.id != link.target.id
        and {link.source.id, link.target.id} <= scoped_ids
    )
    return (*text_groups, *reposts)


def _claim_index(bundle: EvidenceBundle) -> dict[str, list[tuple[str, str]]]:
    fragments = {fragment.id: fragment.record_id for fragment in bundle.fragments}
    links: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
    for link in bundle.links:
        if link.target.type != "finding" or link.relation not in {"supports", "contradicts"}:
            continue
        records = {fragments[fragment_id] for fragment_id in link.fragment_ids}
        if link.source.type == "record":
            records.add(link.source.id)
        elif link.source.type == "fragment":
            records.add(fragments[link.source.id])
        for rid in records:
            links[rid].append((link.target.id, link.relation))
    return links


def _claims(
    record_ids: tuple[str, ...],
    claim_links: dict[str, list[tuple[str, str]]],
    findings: dict[str, tuple[str, str]],
) -> tuple[EventClaim, ...]:
    members: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    for rid in record_ids:
        for link in claim_links.get(rid, ()):
            members[link].add(rid)
    return tuple(
        EventClaim(
            findings[finding_id][0],
            tuple(sorted(members.get((finding_id, "supports"), ()))),
            tuple(sorted(members.get((finding_id, "contradicts"), ()))),
            finding_id=finding_id,
            run_id=findings[finding_id][1],
        )
        for finding_id in sorted({finding_id for finding_id, _ in members})
    )


def _event_points(record_ids: tuple[str, ...], buckets: dict[str, str]) -> tuple[EventPoint, ...]:
    dated: defaultdict[str, list[str]] = defaultdict(list)
    undated = []
    for rid in record_ids:
        if rid in buckets:
            dated[buckets[rid]].append(rid)
        else:
            undated.append(rid)
    points = tuple(EventPoint(label, tuple(ids)) for label, ids in sorted(dated.items()))
    if undated:
        return (*points, EventPoint("Date unknown", tuple(undated)))
    return points


def cluster_events(bundle: EvidenceBundle, dashboard: DashboardProjection) -> EventProjection:
    """Suggest cross-platform events, preserving the exact filtered dashboard record IDs."""
    if dashboard.filters.time_basis != "publication" or dashboard.filters.bucket != "day":
        raise ValueError("Event clustering requires saved publication days.")
    texts = {record.id: record.text or "" for record in bundle.records}
    rows = {row.id: row for row in dashboard.rows}
    if (
        bundle.case.id != dashboard.case_id
        or bundle.fingerprint() != dashboard.case_fingerprint
        or not rows.keys() <= texts.keys()
    ):
        raise ValueError("Event clustering requires the matching saved dashboard case.")
    post_terms = {rid: _post_terms(texts[rid]) for rid in rows}
    days_by_record = {}
    buckets = {}
    for bucket in dashboard.time_buckets:
        day = date.fromisoformat(bucket.start)
        for rid in bucket.record_ids:
            days_by_record[rid] = day
            buckets[rid] = bucket.label
    days = {rid: days_by_record.get(rid) for rid in rows}
    candidates = _wording_candidates(post_terms, days)
    groups = _repeat_groups(bundle, set(rows))
    _attach_repeats(candidates, groups, days)

    group_indices: defaultdict[str, list[int]] = defaultdict(list)
    for index, group in enumerate(groups):
        for rid in group.record_ids:
            group_indices[rid].append(index)
    claim_links = _claim_index(bundle)
    findings = {
        finding.snapshot_id: (finding.statement, finding.run_id) for finding in bundle.findings
    }
    clustered = set()
    events = []
    for candidate in candidates:
        record_ids = tuple(sorted(candidate.record_ids))
        platforms = {rows[rid].platform for rid in record_ids} & {
            "telegram",
            "x",
            "reddit",
            "youtube",
        }
        if len(platforms) < 2:
            continue
        record_set = set(record_ids)
        group_ids = {index for rid in record_ids for index in group_indices[rid]}
        repeat_groups = []
        for index in sorted(group_ids):
            group = groups[index]
            members = tuple(rid for rid in group.record_ids if rid in record_set)
            if len(members) > 1:
                repeat_groups.append(group.model_copy(update={"record_ids": members}))
        matching_terms = tuple(
            word for word in candidate.seed_words if word in candidate.common_terms
        )
        event_id = hashlib.sha256(json.dumps(record_ids).encode("utf-8")).hexdigest()
        events.append(
            EventCluster(
                id=f"event:{event_id}",
                title=" · ".join(matching_terms[:5]).capitalize(),
                matching_terms=matching_terms,
                record_ids=record_ids,
                points=_event_points(record_ids, buckets),
                repeat_groups=tuple(repeat_groups),
                claims=_claims(record_ids, claim_links, findings),
            )
        )
        clustered.update(record_ids)

    events.sort(key=lambda event: (-len(event.record_ids), event.id))
    return EventProjection(tuple(events), tuple(sorted(rows.keys() - clustered)))
