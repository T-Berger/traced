"""Follow every link of an investigation bundle back to its saved record.

The checker reads bundle JSON as plain data, so an edited or broken bundle
still gets a report instead of an exception. It works for any bundle that uses
the shared evidence contract, whatever the investigation type:

1. **Mechanical** (exact): each record's text hashes to its ``content_hash``;
   a Maltego-derived record re-verifies against its frozen file in the
   workspace (all three digests) and its saved-source text; each fragment's
   quote sits at exactly its offsets; every link resolves; every reasoning
   step cites at least one record.
2. **Deterministic reasoning** (heuristic, no model): values a statement
   asserts (dates, domains, emails, IPs, numbers, names) must occur in the
   evidence it cites; a value shown only under an unrelated property (say a
   ``registrar`` line used as proof of an operator) is out of role; a
   ``property: value`` line that names the same property with a different
   date, domain, email, or IP is a conflict; an assumption is backed only
   when a record line names its values under a matching property.
3. **Targeted model checks** (optional): the existing review verifier
   challenges a step against its cited lines, an assumption against the
   most related record lines, and a conclusion against its evidence plus its
   declared assumptions. Objections count only with verbatim quotes.

Every premise link gets one label (worst applies):
``supported`` · ``unchecked`` · ``assumption_not_backed`` · ``unsupported`` ·
``contradicted``. Conclusions list each flagged link they depend on.
Deterministic failures stand; a model check can add a problem or settle an
open question, never clear a deterministic failure.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from evidence_explorer.collection.maltego.records import RecordStore, source_text
from evidence_explorer.review.contracts import EvidenceBundle

from . import CHECKER_VERSION
from .bundle import digest
from .evidence import (
    Line,
    Profile,
    TextIndex,
    compare_lines,
    contains,
    given_index,
    index_lines,
    index_text,
    lines_for_span,
    merge_indexes,
    missing_values,
    occurrences,
    profile,
    relevance,
    role_mismatches,
    specific,
    uncovered_words,
    window,
)
from .reasoning import WINDOW_CHARS, Reasoner, Verdict, Window

LABELS = ("supported", "unchecked", "assumption_not_backed", "unsupported", "contradicted")
SEVERITY = {label: rank for rank, label in enumerate(LABELS)}
CHECKER_METHOD = "traced_investigation_checker"
_HEX64 = re.compile(r"[0-9a-f]{64}")


class FrozenRecordUnavailable(ValueError):
    """The frozen record cannot be read within this check's access or size limits."""


def summarise(reasons: Sequence[str], limit: int = 2) -> list[str]:
    """The first few distinct reasons and a count of the rest."""
    distinct = list(dict.fromkeys(reasons))
    if len(distinct) <= limit:
        return distinct
    return [
        *distinct[:limit],
        f"and {len(distinct) - limit} more record lines disagree the same way",
    ]


def worst(labels: Sequence[str]) -> str:
    return max(labels, key=SEVERITY.__getitem__) if labels else "supported"


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class RecordCheck:
    id: str
    label: str
    title: str | None
    maltego_record_id: str | None
    hash_ok: bool
    frozen: str  # verified · mismatch · failed · unavailable · not_checked · not_maltego
    detail: str
    characters: int

    @property
    def usable(self) -> bool:
        return self.hash_ok and self.frozen not in {"mismatch", "failed"}


@dataclass
class FragmentCheck:
    id: str
    record_id: str | None
    start: int | None
    end: int | None
    quote: str | None
    ok: bool
    detail: str


@dataclass(frozen=True)
class Span:
    record_id: str
    start: int
    end: int
    quote: str
    line: int | None = None
    key: str | None = None


@dataclass
class LinkCheck:
    id: str
    relation: str
    source: dict
    target: dict
    origin: str  # "bundle" or "checker"
    scope: str  # "chain" (claims, steps, assumptions), "collection", or "other"
    label: str = "unchecked"
    basis: str = "deterministic"
    fragment_ids: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)

    def note(self, check: str, result: str, detail: str = "") -> None:
        self.checks.append({"check": check, "result": result, "detail": detail})

    def flag(self, label: str, reason: str, basis: str | None = None) -> None:
        """Raise the label to ``label`` unless a worse one is already set."""
        if self.label in {"unchecked", "supported"} or SEVERITY[label] > SEVERITY[self.label]:
            self.label = label
            if basis:
                self.basis = basis
        if reason and reason not in self.reasons:
            self.reasons.append(reason)


@dataclass
class FindingCheck:
    id: str
    kind: str
    role: str  # claim · step · assumption · observation
    label: str
    statement: str
    verdict: str = "unchecked"
    basis: str = "deterministic"
    problems: list[dict] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    backing: list[dict] = field(default_factory=list)
    contradictions: list[dict] = field(default_factory=list)
    model_notes: list[str] = field(default_factory=list)

    def problem(self, code: str, label: str, message: str, evidence: Sequence[dict] = ()) -> None:
        self.problems.append(
            {"code": code, "label": label, "message": message, "evidence": list(evidence)}
        )


@dataclass
class Conclusion:
    id: str
    label: str
    statement: str
    status: str  # holds · weak · undermined
    flagged: list[dict]
    depends_on: list[str]
    caveats: list[dict] = field(default_factory=list)


@dataclass
class CheckReport:
    schema: str
    checked_at: str
    checker: dict
    bundle: dict
    records: list[RecordCheck]
    fragments: list[FragmentCheck]
    links: list[LinkCheck]
    findings: list[FindingCheck]
    conclusions: list[Conclusion]
    notes: list[str]
    summary: dict
    checked_bundle: dict | None = None

    def to_json(self) -> dict:
        data = asdict(self)
        data.pop("checked_bundle")
        return data


class _Checker:
    def __init__(
        self,
        data: Mapping[str, Any],
        store: RecordStore | None,
        reasoner: Reasoner | None,
        now: str | None,
        model_finding_ids: set[str] | None,
    ):
        self.data = data
        self.store = store
        self.reasoner = reasoner or Reasoner(None)
        self.model_finding_ids = model_finding_ids
        self.now = now or _now()
        self.notes: list[str] = []
        self.records: dict[str, dict] = {}
        self.record_checks: dict[str, RecordCheck] = {}
        self.fragments: dict[str, dict] = {}
        self.fragment_checks: dict[str, FragmentCheck] = {}
        self.findings: dict[str, dict] = {}
        self.finding_checks: dict[str, FindingCheck] = {}
        self.links: list[dict] = []
        self.link_checks: dict[str, LinkCheck] = {}
        self.extra_links: list[LinkCheck] = []
        self.extra_fragments: dict[tuple[str, int, int], dict] = {}
        self.lines: dict[str, tuple[Line, ...]] = {}

    def _model_enabled(self, finding: FindingCheck) -> bool:
        return self.reasoner.verifier is not None and (
            self.model_finding_ids is None or finding.id in self.model_finding_ids
        )

    # -- structure -------------------------------------------------------
    def _items(self, name: str, required: Sequence[str]) -> list[dict]:
        items = self.data.get(name)
        if not isinstance(items, list):
            self.notes.append(f"The bundle has no '{name}' list.")
            return []
        kept = []
        for position, item in enumerate(items):
            if isinstance(item, dict) and all(key in item for key in required):
                kept.append(item)
            else:
                self.notes.append(f"{name}[{position}] is malformed and was skipped.")
        return kept

    def load(self) -> None:
        for record in self._items("records", ("id", "content_hash")):
            self.records.setdefault(str(record["id"]), record)
        for fragment in self._items("fragments", ("id", "record_id", "selector")):
            self.fragments.setdefault(str(fragment["id"]), fragment)
        for finding in self._items("findings", ("snapshot_id", "kind", "statement")):
            self.findings.setdefault(str(finding["snapshot_id"]), finding)
        self.links = self._items("links", ("id", "source", "target", "relation"))
        case = self.data.get("case") if isinstance(self.data.get("case"), dict) else {}
        subject_values = []
        for run in self.data.get("runs") or []:
            if isinstance(run, dict) and isinstance(run.get("configuration"), dict):
                subject = run["configuration"].get("subject")
                if isinstance(subject, dict) and isinstance(subject.get("entity_value"), str):
                    subject_values.append(subject["entity_value"])
        self.case = case
        self.given: TextIndex = given_index(
            [str(case.get("title") or ""), str(case.get("question") or ""), *subject_values]
        )
        self.labels = self._record_labels()

    def _record_labels(self) -> dict[str, str]:
        native_labels: dict[str, str] = {}
        saved_labels: dict[str, str] = {}
        for run in self.data.get("runs") or []:
            configuration = run.get("configuration") if isinstance(run, dict) else None
            if isinstance(configuration, dict) and isinstance(
                configuration.get("record_labels"), dict
            ):
                if configuration.get("record_label_basis") == "record_id":
                    # Labels belong to one draft's selected inputs. Mixing R1
                    # from several drafts would give different records the same
                    # display label. Prefer the latest saved draft's mapping.
                    saved_labels = {
                        str(record_id): str(label)
                        for label, record_id in configuration["record_labels"].items()
                        if str(record_id) in self.records
                    }
                else:
                    for label, native in configuration["record_labels"].items():
                        native_labels.setdefault(str(native), str(label))
        labels: dict[str, str] = {}
        used = set(native_labels.values()) | set(saved_labels.values())
        counter = 0
        for record_id, record in self.records.items():
            if record_id in saved_labels:
                labels[record_id] = saved_labels[record_id]
                continue
            native = (record.get("metadata") or {}).get("native_id")
            if native in native_labels and native_labels[native] not in saved_labels.values():
                labels[record_id] = native_labels[native]
                continue
            counter += 1
            while f"R{counter}" in used:
                counter += 1
            labels[record_id] = f"R{counter}"
            used.add(labels[record_id])
        return labels

    # -- mechanical checks ----------------------------------------------
    def check_records(self) -> None:
        for record_id, record in self.records.items():
            text = record.get("text")
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            native = metadata.get("native_id")
            provenance = (
                metadata.get("provenance") if isinstance(metadata.get("provenance"), dict) else {}
            )
            is_maltego = (
                provenance.get("method") == "maltego_datamcp"
                or metadata.get("platform") == "maltego"
            )
            hash_ok = isinstance(text, str) and (
                hashlib.sha256(text.encode("utf-8")).hexdigest() == record.get("content_hash")
            )
            detail = (
                "text hashes to content_hash"
                if hash_ok
                else (
                    "no saved text"
                    if not isinstance(text, str)
                    else "text does not hash to content_hash"
                )
            )
            frozen, frozen_detail = "not_maltego", ""
            if is_maltego and isinstance(native, str) and _HEX64.fullmatch(native):
                frozen, frozen_detail = self._frozen(native, text)
            elif is_maltego:
                frozen, frozen_detail = "unavailable", "no frozen record ID in metadata.native_id"
            self.record_checks[record_id] = RecordCheck(
                id=record_id,
                label=self.labels[record_id],
                title=metadata.get("title"),
                maltego_record_id=native if is_maltego else None,
                hash_ok=hash_ok,
                frozen=frozen,
                detail="; ".join(part for part in (detail, frozen_detail) if part),
                characters=len(text) if isinstance(text, str) else 0,
            )
            if isinstance(text, str):
                self.lines[record_id] = index_lines(text)

    def _frozen(self, native: str, text: Any) -> tuple[str, str]:
        if self.store is None:
            return "not_checked", "no workspace record store was given"
        path = self.store.path(native)
        if not path.exists():
            return "unavailable", f"frozen record {native[:12]}… is not in this workspace"
        try:
            frozen = self.store.load(native)
        except FrozenRecordUnavailable as exc:
            return "unavailable", str(exc)
        except Exception:  # CollectionFailure, decode errors: a failed check is the result
            return "failed", f"frozen record {native[:12]}… failed its integrity check"
        if source_text(frozen) != text:
            return "mismatch", f"saved text differs from frozen record {native[:12]}…"
        return "verified", f"matches frozen record {native[:12]}… (all digests re-verified)"

    def check_fragments(self) -> None:
        for fragment_id, fragment in self.fragments.items():
            record_id = str(fragment.get("record_id"))
            selector = (
                fragment.get("selector") if isinstance(fragment.get("selector"), dict) else {}
            )
            quote = fragment.get("quote")
            start, end = selector.get("start"), selector.get("end")
            check = FragmentCheck(fragment_id, record_id, start, end, quote, False, "")
            self.fragment_checks[fragment_id] = check
            record = self.records.get(record_id)
            if record is None:
                check.detail = "its record is not in the bundle"
                continue
            if selector.get("kind", "text") != "text":
                check.detail = f"{selector.get('kind')} selectors are not text quotes"
                continue
            text = record.get("text")
            if not isinstance(text, str) or not isinstance(start, int) or not isinstance(end, int):
                check.detail = "a text quote needs saved text and integer offsets"
                continue
            if not 0 <= start < end <= len(text):
                check.detail = f"offsets {start}–{end} fall outside the {len(text)}-character text"
                continue
            if not isinstance(quote, str) or text[start:end] != quote:
                found = occurrences(text, quote) if isinstance(quote, str) else []
                check.detail = (
                    f"the quote is not at {start}–{end}; it occurs at "
                    + ", ".join(f"{index}–{index + len(quote)}" for index in found[:3])
                    if found
                    else f"the quote is not at {start}–{end} and occurs nowhere in the record"
                )
                continue
            if not self.record_checks[record_id].usable:
                check.detail = (
                    f"exact quote, but its record failed: {self.record_checks[record_id].detail}"
                )
                continue
            check.ok = True
            check.detail = f"exact quote at {start}–{end}"

    # -- roles and graph ------------------------------------------------
    def build_graph(self) -> None:
        self.premises: dict[str, list[dict]] = {fid: [] for fid in self.findings}
        self.evidence_links: dict[str, list[dict]] = {fid: [] for fid in self.findings}
        self.dependents: dict[str, list[str]] = {fid: [] for fid in self.findings}
        for link in self.links:
            source, target = link.get("source") or {}, link.get("target") or {}
            relation = link.get("relation")
            scope = "other"
            if (
                relation in {"supports", "depends_on", "contradicts"}
                and target.get("type") == "finding"
                and target.get("id") in self.findings
            ):
                kind = self.findings[target["id"]].get("kind")
                scope = "collection" if kind == "observation" else "chain"
            check = LinkCheck(
                id=str(link["id"]),
                relation=str(relation),
                source=dict(source),
                target=dict(target),
                origin="bundle",
                scope=scope,
                fragment_ids=[str(fid) for fid in link.get("fragment_ids") or []],
            )
            self.link_checks[check.id] = check
            if relation not in {"supports", "depends_on"} or scope == "other":
                continue
            target_id = target["id"]
            if source.get("type") == "finding" and source.get("id") in self.findings:
                self.premises[target_id].append(link)
                self.dependents[source["id"]].append(target_id)
            elif source.get("type") in {"record", "fragment"}:
                self.evidence_links[target_id].append(link)
        for finding_id, finding in self.findings.items():
            details = finding.get("details") if isinstance(finding.get("details"), dict) else {}
            kind = str(finding.get("kind"))
            if kind == "assumption":
                role = "assumption"
            elif kind == "observation":
                role = "observation"
            elif details.get("role") in {"claim", "step"}:
                role = details["role"]
            else:
                role = "step" if self.dependents[finding_id] else "claim"
            self.finding_checks[finding_id] = FindingCheck(
                id=finding_id,
                kind=kind,
                role=role,
                label=str(details.get("label") or ""),
                statement=str(finding.get("statement") or ""),
            )
        counters = {"claim": 0, "step": 0, "assumption": 0, "observation": 0}
        prefix = {"claim": "C", "step": "S", "assumption": "A", "observation": "O"}
        for check in self.finding_checks.values():
            if not check.label:
                counters[check.role] += 1
                check.label = f"{prefix[check.role]}{counters[check.role]}"

    # -- helpers --------------------------------------------------------
    def spans_of(self, link: Mapping[str, Any], link_check: LinkCheck) -> list[Span]:
        """Mechanically check one evidence link and return its valid quote spans."""
        source = link.get("source") or {}
        fragment_ids = [str(fid) for fid in link.get("fragment_ids") or []]
        if source.get("type") == "fragment":
            fragment_ids = list(dict.fromkeys([str(source.get("id")), *fragment_ids]))
        spans: list[Span] = []
        source_record = source.get("id") if source.get("type") == "record" else None
        if source_record is not None and source_record not in self.records:
            link_check.note(
                "source_resolves", "fail", f"record {source_record} is not in the bundle"
            )
            link_check.flag("unsupported", "cites a record that is not in the bundle", "mechanical")
            return []
        if not fragment_ids:
            if source_record is None:
                link_check.note("cites_text", "fail", "no fragment and no record")
                link_check.flag("unsupported", "the link cites no saved text", "mechanical")
                return []
            record_check = self.record_checks[source_record]
            text = self.records[source_record].get("text")
            if not record_check.usable or not isinstance(text, str):
                link_check.note("record_hash", "fail", record_check.detail)
                link_check.flag(
                    "unsupported",
                    f"record {record_check.label} failed: {record_check.detail}",
                    "mechanical",
                )
                return []
            link_check.note("whole_record", "pass", f"cites all of {record_check.label}")
            return [Span(source_record, 0, len(text), text)]
        for fragment_id in fragment_ids:
            fragment_check = self.fragment_checks.get(fragment_id)
            if fragment_check is None:
                link_check.note(
                    "fragment_resolves", "fail", f"fragment {fragment_id} is not in the bundle"
                )
                link_check.flag(
                    "unsupported", f"cites missing fragment {fragment_id}", "mechanical"
                )
                continue
            label = self.labels.get(fragment_check.record_id or "", "?")
            if source_record is not None and fragment_check.record_id != source_record:
                link_check.note("fragment_record", "fail", "fragment belongs to another record")
                link_check.flag(
                    "unsupported", "a cited fragment belongs to another record", "mechanical"
                )
                continue
            if not fragment_check.ok:
                link_check.note("quote_at_offsets", "fail", f"{label}: {fragment_check.detail}")
                link_check.flag(
                    "unsupported",
                    f"broken citation in {label}: {fragment_check.detail}",
                    "mechanical",
                )
                continue
            record_check = self.record_checks[fragment_check.record_id]
            link_check.note("quote_at_offsets", "pass", f"{label} {fragment_check.detail}")
            link_check.note("record_hash", "pass", f"{label}: {record_check.detail}")
            lines = lines_for_span(
                self.lines[fragment_check.record_id], fragment_check.start, fragment_check.end
            )
            spans.append(
                Span(
                    fragment_check.record_id,
                    fragment_check.start,
                    fragment_check.end,
                    fragment_check.quote,
                    lines[0].number if lines else None,
                    lines[0].key if lines else None,
                )
            )
        return spans

    def span_lines(self, spans: Sequence[Span]) -> list[Line]:
        found: dict[tuple[str, int], Line] = {}
        for span in spans:
            for line in lines_for_span(self.lines[span.record_id], span.start, span.end):
                found[(span.record_id, line.number)] = line
        return list(found.values())

    def all_lines(self) -> list[tuple[str, Line]]:
        return [
            (record_id, line)
            for record_id, lines in self.lines.items()
            if self.record_checks[record_id].usable
            for line in lines
        ]

    def line_span(self, record_id: str, line: Line) -> dict:
        return self.describe_span(
            Span(record_id, line.start, line.end, line.text, line.number, line.key)
        )

    def describe_span(self, span: Span) -> dict:
        lines = lines_for_span(self.lines.get(span.record_id, ()), span.start, span.end)
        quote = span.quote if len(span.quote) <= 300 else span.quote[:297] + "…"
        record_check = self.record_checks.get(span.record_id)
        return {
            "record_id": span.record_id,
            "record": self.labels.get(span.record_id),
            "maltego_record_id": record_check.maltego_record_id if record_check else None,
            "start": span.start,
            "end": span.end,
            "line": span.line if span.line is not None else (lines[0].number if lines else None),
            "key": span.key if span.key is not None else (lines[0].key if lines else None),
            "quote": quote,
        }

    def line_window(
        self, record_id: str, line: Line, focus: tuple[int, int] | None = None
    ) -> Window:
        text = self.records[record_id]["text"]
        start, end = focus or (line.start, line.end)
        if line.end - line.start <= WINDOW_CHARS:
            left, right = line.start, line.end
        else:
            left, right = window(text, start, end, WINDOW_CHARS)
            left, right = max(left, line.start), min(right, line.end)
        return Window(
            text[left:right], record_id, left, right, (left, right) != (line.start, line.end)
        )

    def add_span_link(
        self, relation: str, span: Span, target: str, basis: str, label: str
    ) -> LinkCheck:
        key = (span.record_id, span.start, span.end)
        if key not in self.extra_fragments:
            self.extra_fragments[key] = {
                "id": "check:fragment:" + digest(list(key)),
                "record_id": span.record_id,
                "selector": {"kind": "text", "start": span.start, "end": span.end},
                "quote": self.records[span.record_id]["text"][span.start : span.end],
            }
        fragment_id = self.extra_fragments[key]["id"]
        link_id = "check:link:" + digest([relation, fragment_id, target])
        for existing in self.extra_links:
            if existing.id == link_id:
                return existing
        check = LinkCheck(
            id=link_id,
            relation=relation,
            source={"type": "record", "id": span.record_id},
            target={"type": "finding", "id": target},
            origin="checker",
            scope="chain",
            label=label,
            basis="deterministic",
            fragment_ids=[fragment_id],
            reasons=[basis],
            evidence=[self.describe_span(span)],
        )
        self.extra_links.append(check)
        return check

    def record_contradiction(
        self, finding: FindingCheck, span: Span, reason: str, basis: str
    ) -> None:
        described = self.describe_span(span)
        if any(
            item["record_id"] == described["record_id"] and item["start"] == described["start"]
            for item in finding.contradictions
        ):
            return
        finding.contradictions.append({**described, "reason": reason, "basis": basis})
        link = self.add_span_link("contradicts", span, finding.id, reason, "contradicted")
        link.basis = basis
        if link not in self.contradiction_links.setdefault(finding.id, []):
            self.contradiction_links[finding.id].append(link)

    # -- statement checks ----------------------------------------------
    def carriers(self, finding_id: str, transitive: bool = False) -> list[str]:
        statements, seen, queue = [], set(), [finding_id]
        while queue:
            current = queue.pop()
            for link in self.premises.get(current, []):
                premise = link["source"]["id"]
                if premise in seen:
                    continue
                seen.add(premise)
                statements.append(str(self.findings[premise].get("statement") or ""))
                if transitive:
                    queue.append(premise)
        return statements

    def transitive_assumptions(self, finding_id: str) -> list[str]:
        found, seen, queue = [], set(), [finding_id]
        while queue:
            current = queue.pop()
            for link in self.premises.get(current, []):
                premise = link["source"]["id"]
                if premise in seen:
                    continue
                seen.add(premise)
                if self.finding_checks[premise].role == "assumption":
                    found.append(premise)
                queue.append(premise)
        return found

    def transitive_spans(self, finding_id: str) -> list[Span]:
        spans, seen, queue = [], set(), [finding_id]
        while queue:
            current = queue.pop()
            if current in seen:
                continue
            seen.add(current)
            for link in self.evidence_links.get(current, []):
                spans.extend(self.valid_spans.get(link["id"], []))
            for link in self.premises.get(current, []):
                queue.append(link["source"]["id"])
        return spans

    def check_evidence(self) -> None:
        """Resolve every link; verify the quotes of links that cite saved text."""
        self.valid_spans: dict[str, list[Span]] = {}
        self.contradiction_links: dict[str, list[LinkCheck]] = {}
        pools = {"record": self.records, "fragment": self.fragments, "finding": self.findings}
        for link in self.links:
            check = self.link_checks[link["id"]]
            source, target = link.get("source") or {}, link.get("target") or {}
            resolved = True
            for ref, name in ((source, "source"), (target, "target")):
                pool = pools.get(ref.get("type"))
                if pool is None or ref.get("id") not in pool:
                    resolved = False
                    check.note(
                        f"{name}_resolves",
                        "fail",
                        f"{ref.get('type')} {ref.get('id')} is not in the bundle",
                    )
                    check.flag("unsupported", f"the link's {name} does not resolve", "mechanical")
            if not resolved or source.get("type") not in {"record", "fragment"}:
                continue
            if check.scope not in {"chain", "collection"}:
                continue
            spans = self.spans_of(link, check)
            self.valid_spans[link["id"]] = spans
            if link.get("relation") == "contradicts" and spans:
                finding = self.finding_checks[target["id"]]
                basis = str(link.get("basis") or "declared contradiction")
                for span in spans:
                    finding.contradictions.append(
                        {**self.describe_span(span), "reason": basis, "basis": "bundle"}
                    )
                check.flag("contradicted", f"declared in the bundle: {basis}", "mechanical")
                self.contradiction_links.setdefault(target["id"], []).append(check)

    def statement_problems(
        self,
        finding: FindingCheck,
        statement: Profile,
        spans: Sequence[Span],
        carriers: Sequence[str],
    ) -> tuple[list[dict], list[Line]]:
        """Grounding and role problems of a statement against its cited spans."""
        problems = []
        quote_index = merge_indexes([index_text(span.quote) for span in spans] or [index_text("")])
        carried = merge_indexes([index_text(text) for text in carriers] or [index_text("")])
        missing = [
            value
            for value in missing_values(statement, quote_index)
            if not contains(carried, value)
        ]
        lines = self.span_lines(spans)
        if missing:
            shown = ", ".join(f"'{value.text}'" for value in missing)
            problems.append(
                {
                    "code": "value_not_in_evidence",
                    "label": "unsupported",
                    "message": (
                        f"{finding.label} asserts {shown}, which its cited quotes do not contain"
                    ),
                }
            )
        for value, holding in role_mismatches(statement, lines):
            keys = sorted({line.key for line in holding if line.key})
            problems.append(
                {
                    "code": "out_of_role",
                    "label": "unsupported",
                    "message": (
                        f"'{value.text}' appears in the cited evidence only as "
                        + ", ".join(f"`{key}`" for key in keys)
                        + f", which does not show what {finding.label} says about it"
                    ),
                }
            )
        return problems, lines

    def conflicts(
        self, finding: FindingCheck, statement: Profile, cited: Sequence[Span]
    ) -> list[tuple[Span, str, bool]]:
        """Record lines that contradict the statement on a property they share.

        A cited line that disagrees is a misread and always counts. Any other line
        counts only when no saved line agrees: records often hold several snapshots
        (historical WHOIS), and an older value is not a contradiction of a newer one.
        """
        cited_keys = {
            (span.record_id, line.number)
            for span in cited
            for line in lines_for_span(self.lines[span.record_id], span.start, span.end)
        }
        tagged = [((record_id, line.number), line) for record_id, line in self.all_lines()]
        found = []
        for comparison in compare_lines(statement, tagged):
            own = [item for item in comparison.disagree if item[0] in cited_keys]
            if own:
                chosen = own
            elif not comparison.agree:
                chosen = list(comparison.disagree)
            else:
                self.note_differences(finding, comparison)
                continue
            for (record_id, number), line, shown in chosen:
                span = Span(record_id, line.start, line.end, line.text, line.number, line.key)
                reason = (
                    f"{self.labels[record_id]} line {number} `{line.key}`: the record gives "
                    f"{', '.join(shown)} where {finding.label} says {', '.join(comparison.said)}"
                )
                found.append((span, reason, (record_id, number) in cited_keys))
        return found

    def note_differences(self, finding: FindingCheck, comparison: Any) -> None:
        """Other records or snapshots that give another value while some line agrees."""
        (agree_tag, agree_line), *_ = comparison.agree
        shown = [
            f"{self.labels[record_id]} line {number} `{line.key}` gives {', '.join(values)}"
            for (record_id, number), line, values in comparison.disagree
        ]
        if not shown:
            return
        listed = summarise(shown)
        caveat = (
            "; ".join(listed)
            + f" (another record or snapshot; {self.labels[agree_tag[0]]} line {agree_tag[1]} "
            f"`{agree_line.key}` agrees with {finding.label}); the value may have changed over time"
        )
        if caveat not in finding.caveats:
            finding.caveats.append(caveat)

    def check_steps(self) -> None:
        for finding_id, finding in self.finding_checks.items():
            if finding.kind != "inference":
                continue
            links = self.evidence_links[finding_id]
            spans = [span for link in links for span in self.valid_spans.get(link["id"], [])]
            statement = profile(finding.statement, self.given)
            details = self.findings[finding_id].get("details")
            rejected = details.get("rejected_citations") if isinstance(details, dict) else None
            for item in rejected if isinstance(rejected, list) else []:
                if isinstance(item, dict):
                    finding.problem(
                        "rejected_citation",
                        "unsupported",
                        f"{finding.label} cited {item.get('record')} line {item.get('line')} "
                        f'"{str(item.get("quote"))[:160]}", but {item.get("how")}',
                    )
            if finding.role == "step" and not links:
                finding.problem("cites_nothing", "unsupported", f"{finding.label} cites no record")
            if not links:
                continue
            carriers = self.carriers(finding_id)
            problems, lines = self.statement_problems(finding, statement, spans, carriers)
            conflicts = self.conflicts(finding, statement, spans)
            for span, reason, own in conflicts:
                if own:
                    problems.append(
                        {"code": "cited_line_conflicts", "label": "contradicted", "message": reason}
                    )
                else:
                    self.record_contradiction(finding, span, reason, "deterministic")
            mechanical_ok = all(
                self.link_checks[link["id"]].label != "unsupported" for link in links
            )
            positive = (
                mechanical_ok
                and spans
                and not problems
                and statement.checkable
                and not uncovered_words(statement, lines)
            )
            for link in links:
                check = self.link_checks[link["id"]]
                for problem in problems:
                    check.note(problem["code"], "fail", problem["message"])
                    check.flag(problem["label"], problem["message"], "deterministic")
                if check.label == "unchecked" and not check.reasons:
                    if positive:
                        check.label = "supported"
                        check.note(
                            "deterministic_support",
                            "pass",
                            "values and properties match the cited lines",
                        )
                    else:
                        check.note(
                            "deterministic_support",
                            "open",
                            "no checkable value or property"
                            if not statement.checkable
                            else "the cited lines do not name every property the statement uses: "
                            + ", ".join(uncovered_words(statement, lines)),
                        )
            if not self._model_enabled(finding) or not mechanical_ok or not spans:
                continue
            if any(problem["label"] in {"unsupported", "contradicted"} for problem in problems):
                continue  # deterministic failures stand; skip the model call
            windows = self._cited_windows(spans)
            cited_count = len(windows)
            cited_lines = {
                (span.record_id, line.number)
                for span in spans
                for line in lines_for_span(self.lines[span.record_id], span.start, span.end)
            }
            for record_id, line in self.related_lines(
                statement, exclude=cited_lines, limit=3, minimum=4
            ):
                windows.append(self.line_window(record_id, line))
            verdict = self.reasoner.challenge(finding.statement, windows)
            self.apply_step_verdict(finding, links, verdict, windows[:cited_count])

    def _cited_windows(self, spans: Sequence[Span]) -> list[Window]:
        windows: list[Window] = []
        seen = set()
        for span in spans:
            for line in lines_for_span(self.lines[span.record_id], span.start, span.end):
                key = (span.record_id, line.number)
                if key in seen:
                    continue
                seen.add(key)
                focus = (max(span.start, line.start), min(span.end, line.end))
                windows.append(self.line_window(span.record_id, line, focus))
        return windows

    def related_lines(
        self, statement: Profile, *, exclude: set, limit: int, minimum: int
    ) -> list[tuple[str, Line]]:
        scored = []
        for record_id, line in self.all_lines():
            if not line.text.strip():
                continue
            score = relevance(statement, line)
            if score >= minimum and (record_id, line.number) not in exclude:
                scored.append((-score, self.labels[record_id], line.number, record_id, line))
        scored.sort(key=lambda item: item[:3])
        return [(record_id, line) for *_, record_id, line in scored[:limit]]

    def _span_from(self, item: Mapping[str, Any]) -> Span | None:
        if "record_id" not in item:
            return None
        return Span(item["record_id"], item["start"], item["end"], item["quote"])

    def apply_step_verdict(
        self, finding: FindingCheck, links, verdict: Verdict, cited: Sequence[Window]
    ) -> None:
        finding.model_notes.extend(
            f"discarded model objection — {item}" for item in verdict.discarded
        )
        if verdict.status != "ok":
            if verdict.error:
                finding.model_notes.append(f"model check unavailable: {verdict.error}")
            return
        cited_ranges = [(w.record_id, w.start, w.end) for w in cited]

        def in_cited(span: Span) -> bool:
            return any(
                r == span.record_id and s <= span.start and span.end <= e
                for r, s, e in cited_ranges
            )

        for objection in verdict.of("contradiction"):
            spans = [span for span in map(self._span_from, objection.spans) if span]
            other = [span for span in spans if not in_cited(span)]
            if other:
                for span in other:
                    self.record_contradiction(finding, span, objection.explanation, "model")
            else:
                for link in links:
                    check = self.link_checks[link["id"]]
                    check.note("model_check", "fail", objection.explanation)
                    check.flag("contradicted", f"model: {objection.explanation}", "model")
        for objection in verdict.of("unsupported_inference", "insufficient_evidence"):
            for link in links:
                check = self.link_checks[link["id"]]
                check.note("model_check", "fail", objection.explanation)
                check.flag(
                    "unsupported",
                    f"model ({objection.kind.replace('_', ' ')}): {objection.explanation}",
                    "model",
                )
        for objection in verdict.of("alternative_explanation"):
            finding.caveats.append(f"model: {objection.explanation}")
        if not verdict.discarded and not verdict.of(
            "contradiction", "unsupported_inference", "insufficient_evidence"
        ):
            for link in links:
                check = self.link_checks[link["id"]]
                if check.label == "unchecked":
                    check.label = "supported"
                    check.basis = "model"
                check.note(
                    "model_check",
                    "pass",
                    "the verifier raised no objection against the cited lines",
                )

    def check_assumptions(self) -> None:
        all_lines = self.all_lines()
        for finding_id, finding in self.finding_checks.items():
            if finding.role != "assumption":
                continue
            statement = profile(finding.statement, self.given)
            verdict, reasons, basis = "assumption_not_backed", [], "deterministic"
            for span, reason, _ in self.conflicts(finding, statement, []):
                self.record_contradiction(finding, span, reason, "deterministic")
            backing_lines = []
            if statement.values:
                for record_id, line in all_lines:
                    if any(contains(line.index, value) for value in statement.values) and (
                        not statement.roles or specific(line.concepts) & statement.roles
                    ):
                        backing_lines.append((record_id, line))
            union = merge_indexes([line.index for _, line in backing_lines] or [index_text("")])
            missing = missing_values(statement, union)
            mismatches = role_mismatches(statement, [line for _, line in all_lines])
            uncovered = uncovered_words(statement, [line for _, line in backing_lines])
            if finding.contradictions:
                verdict = "contradicted"
                reasons = summarise([item["reason"] for item in finding.contradictions])
            elif backing_lines and not missing and not mismatches and not uncovered:
                verdict = "supported"
                finding.backing = [
                    self.line_span(record_id, line) for record_id, line in backing_lines[:4]
                ]
                reasons = ["record lines name its values under a matching property"]
            else:
                if mismatches:
                    for value, holding in mismatches:
                        keys = sorted({line.key for line in holding if line.key})
                        reasons.append(
                            f"'{value.text}' appears in the records only as "
                            + ", ".join(f"`{key}`" for key in keys)
                        )
                absent = [
                    v
                    for v in statement.values
                    if not any(contains(line.index, v) for _, line in all_lines)
                ]
                if absent:
                    reasons.append("no record contains " + ", ".join(f"'{v.text}'" for v in absent))
                if uncovered and backing_lines:
                    keys = sorted({line.key for _, line in backing_lines if line.key})
                    reasons.append(
                        "its values appear only as "
                        + ", ".join(f"`{key}`" for key in keys[:4])
                        + "; no record line states what it says about "
                        + ", ".join(f"'{word}'" for word in uncovered)
                    )
                if not reasons:
                    reasons.append("no record line states it")
            candidates = self.related_lines(statement, exclude=set(), limit=6, minimum=2)
            if self._model_enabled(finding) and candidates and verdict != "contradicted":
                windows = [self.line_window(record_id, line) for record_id, line in candidates]
                model = self.reasoner.challenge(finding.statement, windows)
                finding.model_notes.extend(
                    f"discarded model objection — {item}" for item in model.discarded
                )
                if model.status == "ok":
                    contradictions = model.of("contradiction")
                    for objection in contradictions:
                        for span in filter(None, map(self._span_from, objection.spans)):
                            self.record_contradiction(finding, span, objection.explanation, "model")
                    if finding.contradictions:
                        verdict, basis = "contradicted", "model"
                        reasons = summarise([item["reason"] for item in finding.contradictions])
                    else:
                        doubts = model.of("unsupported_inference", "insufficient_evidence")
                        if doubts:
                            if verdict == "supported":
                                verdict, basis = "assumption_not_backed", "model"
                                finding.backing = []
                                reasons = []
                            reasons.extend(f"model: {item.explanation}" for item in doubts)
                        elif not model.discarded and verdict != "supported":
                            reasons.append(
                                "the verifier raised no objection, but no record line states it"
                            )
                        finding.caveats.extend(
                            f"model: {item.explanation}"
                            for item in model.of("alternative_explanation")
                        )
                elif model.error:
                    finding.model_notes.append(f"model check unavailable: {model.error}")
            finding.verdict, finding.basis = verdict, basis
            if verdict != "supported":
                for reason in reasons:
                    finding.problem("backing", verdict, reason)
            for link in self.links:
                if (
                    link.get("relation") == "depends_on"
                    and (link.get("source") or {}).get("id") == finding_id
                ):
                    check = self.link_checks[link["id"]]
                    if check.label == "unsupported":
                        continue
                    check.label = verdict
                    check.basis = basis
                    check.reasons.extend(
                        reason for reason in reasons if reason not in check.reasons
                    )
                    check.note(
                        "assumption_backing",
                        "pass" if verdict == "supported" else "fail",
                        "; ".join(reasons),
                    )
                    if finding.backing:
                        check.evidence = list(finding.backing)
                    elif finding.contradictions:
                        check.evidence = [
                            {
                                key: value
                                for key, value in item.items()
                                if key not in {"reason", "basis"}
                            }
                            for item in finding.contradictions
                        ]
            for backing in finding.backing:
                span = Span(
                    backing["record_id"], backing["start"], backing["end"], backing["quote"]
                )
                self.add_span_link(
                    "supports",
                    span,
                    finding_id,
                    "Checker: this record line backs the assumption.",
                    "supported",
                )

    def check_conclusions(self) -> None:
        for finding_id, finding in self.finding_checks.items():
            if finding.role != "claim" or finding.kind != "inference":
                continue
            premise_links = self.premises[finding_id]
            step_links = [link for link in premise_links if link.get("relation") == "supports"]
            assumptions = self.transitive_assumptions(finding_id)
            if not premise_links and not self.evidence_links[finding_id]:
                finding.problem(
                    "unsupported_conclusion",
                    "unsupported",
                    f"{finding.label} has no steps, evidence, or assumptions",
                )
                continue
            statement = profile(finding.statement, self.given)
            spans = self.transitive_spans(finding_id)
            carriers = self.carriers(finding_id, transitive=True)
            quote_index = merge_indexes(
                [index_text(span.quote) for span in spans] or [index_text("")]
            )
            carried = merge_indexes([index_text(text) for text in carriers] or [index_text("")])
            missing = [
                v for v in missing_values(statement, quote_index) if not contains(carried, v)
            ]
            problems = []
            if missing:
                problems.append(
                    f"{finding.label} introduces "
                    + ", ".join(f"'{value.text}'" for value in missing)
                    + ", which none of its steps, quotes, or assumptions contain"
                )
            lines = self.span_lines(spans)
            assumption_index = merge_indexes(
                [index_text(self.finding_checks[a].statement) for a in assumptions]
                or [index_text("")]
            )
            assumption_roles = frozenset().union(
                *(profile(self.finding_checks[a].statement).roles for a in assumptions), frozenset()
            )
            for value, holding in role_mismatches(statement, lines):
                if contains(assumption_index, value) or (statement.roles & assumption_roles):
                    continue  # a declared assumption carries the reinterpretation
                keys = sorted({line.key for line in holding if line.key})
                problems.append(
                    f"{finding.label} treats '{value.text}' (shown only as "
                    + ", ".join(f"`{key}`" for key in keys)
                    + ") in a role its evidence does not show, without a stated assumption"
                )
            for span, reason, _ in self.conflicts(finding, statement, spans):
                self.record_contradiction(finding, span, reason, "deterministic")
            premise_concepts = frozenset().union(
                *(profile(text).concepts for text in carriers),
                *(line.concepts for line in lines),
                frozenset(),
            )
            uncovered = [
                word
                for word, concepts in statement.words.items()
                if specific(concepts) and not specific(concepts) & premise_concepts
            ]
            positive = statement.checkable and not problems and not uncovered and bool(step_links)
            if not statement.checkable:
                open_reason = "no checkable value or property"
            elif uncovered:
                open_reason = "the steps do not name: " + ", ".join(uncovered)
            else:
                open_reason = "not decided"
            for link in step_links:
                check = self.link_checks[link["id"]]
                for problem in problems:
                    check.note("inference", "fail", problem)
                    check.flag("unsupported", problem, "deterministic")
                if positive and check.label == "unchecked":
                    check.label = "supported"
                    check.note(
                        "inference",
                        "pass",
                        "the conclusion's values and properties come from its steps",
                    )
                elif not problems:
                    check.note("inference", "open", open_reason)
            if problems or not self._model_enabled(finding) or not spans:
                continue
            windows = self._cited_windows(spans)
            for assumption_id in assumptions:
                windows.append(
                    Window(
                        self.finding_checks[assumption_id].statement,
                        note="Declared assumption (not evidence): ",
                    )
                )
            verdict = self.reasoner.challenge(finding.statement, windows)
            finding.model_notes.extend(
                f"discarded model objection — {item}" for item in verdict.discarded
            )
            if verdict.status != "ok":
                if verdict.error:
                    finding.model_notes.append(f"model check unavailable: {verdict.error}")
                continue
            for objection in verdict.of("contradiction"):
                record_spans = [span for span in map(self._span_from, objection.spans) if span]
                for span in record_spans:
                    self.record_contradiction(finding, span, objection.explanation, "model")
            doubts = verdict.of("unsupported_inference", "insufficient_evidence")
            for link in step_links:
                check = self.link_checks[link["id"]]
                for objection in doubts:
                    check.note("model_check", "fail", objection.explanation)
                    check.flag(
                        "unsupported",
                        f"model ({objection.kind.replace('_', ' ')}): {objection.explanation}",
                        "model",
                    )
                if not doubts and not verdict.discarded and check.label == "unchecked":
                    check.label, check.basis = "supported", "model"
                    check.note(
                        "model_check", "pass", "the verifier raised no objection to this inference"
                    )
            finding.caveats.extend(
                f"model: {item.explanation}" for item in verdict.of("alternative_explanation")
            )

    def check_collection_links(self) -> None:
        for link in self.links:
            check = self.link_checks[link["id"]]
            if check.scope == "chain":
                continue
            if (
                check.scope == "collection"
                and link["id"] in self.valid_spans
                and check.label != "unsupported"
            ):
                check.label, check.basis = "supported", "mechanical"
                check.note("collection", "pass", "source-collection link: records resolve and hash")
            elif check.scope == "other" and check.label != "unsupported":
                ok = (
                    all(
                        ref.get("type") == "record"
                        and ref.get("id") in self.records
                        and self.record_checks[ref["id"]].usable
                        for ref in (check.source, check.target)
                    )
                    if check.relation in {"same_content", "documented_repost", "similar_wording"}
                    else True
                )
                check.label, check.basis = ("supported" if ok else "unsupported"), "mechanical"
                check.note(
                    "record_relationship",
                    "pass" if ok else "fail",
                    "records resolve and hash" if ok else "a related record failed",
                )

    def unused_assumptions(self) -> None:
        for finding in self.finding_checks.values():
            if finding.role == "assumption" and not self.dependents[finding.id]:
                self.notes.append(f"Assumption {finding.label} is not used by any step or claim.")

    # -- propagation -----------------------------------------------------
    def trace(self) -> list[Conclusion]:
        conclusions = [
            self._trace_one(finding_id)
            for finding_id, finding in self.finding_checks.items()
            if finding.role == "claim"
        ]
        conclusions.sort(key=lambda item: item.label)
        return conclusions

    def _trace_one(self, finding_id: str) -> Conclusion:
        """Walk a conclusion's premises and collect every link that is not supported."""
        finding = self.finding_checks[finding_id]
        flagged: list[dict] = []
        seen_items: set[tuple[str, str]] = set()
        depends: set[str] = set()

        def add(item: str, item_id: str, label: str, reason: str, path: list[str], evidence=()):
            if label == "supported" or (item, item_id) in seen_items:
                return
            seen_items.add((item, item_id))
            flagged.append(
                {
                    "item": item,
                    "id": item_id,
                    "label": label,
                    "reason": reason,
                    "via": path,
                    "evidence": list(evidence),
                }
            )

        caveats: list[dict] = []
        stack: list[tuple[str, list[str], frozenset[str]]] = [
            (finding_id, [finding.label], frozenset({finding_id}))
        ]
        while stack:
            current, path, visited = stack.pop()
            node = self.finding_checks[current]
            for caveat in node.caveats:
                if not any(item["caveat"] == caveat for item in caveats):
                    caveats.append({"via": path, "caveat": caveat})
            if node.role != "assumption":  # an assumption's verdict rides on its link
                for problem in node.problems:
                    add("finding", current, problem["label"], problem["message"], path)
                contradictions = self.contradiction_links.get(current, [])
                if contradictions:  # one entry per finding, however many lines disagree
                    reasons = summarise([r for c in contradictions for r in c.reasons])
                    add(
                        "link",
                        contradictions[0].id,
                        "contradicted",
                        "; ".join(reasons),
                        path,
                        [item for c in contradictions for item in c.evidence],
                    )
            for link in self.evidence_links[current]:
                check = self.link_checks[link["id"]]
                reason = "; ".join(check.reasons) or self._open_reason(check)
                add("link", check.id, check.label, reason, path, check.evidence)
            for link in self.premises[current]:
                premise = link["source"]["id"]
                check = self.link_checks[link["id"]]
                premise_check = self.finding_checks[premise]
                if premise_check.role == "assumption":
                    depends.add(premise_check.label)
                step_path = [*path, premise_check.label]
                reason = "; ".join(check.reasons) or self._open_reason(check)
                add("link", check.id, check.label, reason, step_path, check.evidence)
                if premise not in visited:
                    stack.append((premise, step_path, visited | {premise}))
        labels = [item["label"] for item in flagged]
        if any(label in {"unsupported", "contradicted"} for label in labels):
            status = "undermined"
        elif labels:
            status = "weak"
        else:
            status = "holds"
        flagged.sort(key=lambda item: (-SEVERITY[item["label"]], len(item["via"])))
        return Conclusion(
            finding_id,
            finding.label,
            finding.statement,
            status,
            flagged,
            sorted(depends),
            caveats,
        )

    @staticmethod
    def _open_reason(check: LinkCheck) -> str:
        opened = [
            item["detail"] for item in check.checks if item["result"] == "open" and item["detail"]
        ]
        not_run = [item["detail"] for item in check.checks if item["result"] == "not_run"]
        reason = opened[0] if opened else "not decided by any check"
        return f"{reason}; model check not run: {not_run[0]}" if not_run else reason

    def mark_model_not_run(self) -> int:
        """Say on every open link that the model check did not run, and why."""
        reason = self.reasoner.not_run_reason
        if self.reasoner.verifier is not None or not reason:
            return 0
        left_open = 0
        for check in [*self.link_checks.values(), *self.extra_links]:
            if check.scope != "chain" or check.label not in {"unchecked", "assumption_not_backed"}:
                continue
            check.note("model_check", "not_run", reason)
            left_open += check.label == "unchecked"
        return left_open

    # -- output -----------------------------------------------------------
    def checked_bundle(self, summary: Mapping[str, Any]) -> dict | None:
        try:
            EvidenceBundle.model_validate(self.data)
        except ValidationError:
            return None
        bundle = {key: value for key, value in self.data.items()}
        for name in ("runs", "fragments", "links"):
            bundle[name] = list(bundle.get(name) or [])
        run_id = "check:run:" + digest(
            [self.fingerprint, CHECKER_VERSION, self.reasoner.describe()["model"], self.now]
        )
        bundle["runs"].append(
            {
                "id": run_id,
                "input_fingerprint": self.fingerprint,
                "input_manifest": sorted(self.records),
                "method": CHECKER_METHOD,
                "method_version": CHECKER_VERSION,
                "created_at": self.now,
                "full_inputs_available": True,
                "recomputation_capability": "dependency_review_only",
                "configuration": {"reasoning": self.reasoner.describe()},
                "coverage": {"links": summary["links"], "conclusions": summary["conclusions"]},
                "status": "completed",
            }
        )
        bundle["fragments"].extend(self.extra_fragments.values())
        for check in self.extra_links:
            bundle["links"].append(
                {
                    "id": check.id,
                    "source": check.source,
                    "target": check.target,
                    "relation": check.relation,
                    "fragment_ids": list(check.fragment_ids),
                    "basis": "; ".join(check.reasons)[:2000] or "Checker link.",
                    "run_id": run_id,
                    "check_label": check.label,
                }
            )
        try:
            return EvidenceBundle.model_validate(bundle).model_dump(mode="json")
        except ValidationError as exc:
            self.notes.append(
                f"The checked bundle did not validate and was not written: {exc.errors()[0]['msg']}"
            )
            return None

    def run(self) -> CheckReport:
        self.load()
        try:
            self.fingerprint = EvidenceBundle.model_validate(self.data).fingerprint()
            contract = {"valid": True, "detail": "passes EvidenceBundle validation"}
        except ValidationError as exc:
            first = exc.errors()[0]
            self.fingerprint = digest(self.data)
            contract = {
                "valid": False,
                "detail": f"fails EvidenceBundle validation: {first['msg']}",
            }
        self.check_records()
        self.check_fragments()
        self.build_graph()
        self.check_evidence()
        self.check_assumptions()
        self.check_steps()
        self.check_conclusions()
        self.check_collection_links()
        self.unused_assumptions()
        for finding in self.finding_checks.values():
            if finding.role in {"step", "claim"}:
                labels = [
                    self.link_checks[link["id"]].label for link in self.evidence_links[finding.id]
                ]
                labels += [problem["label"] for problem in finding.problems]
                if finding.contradictions:
                    labels.append("contradicted")
                if finding.role == "claim":
                    labels += [
                        self.link_checks[link["id"]].label
                        for link in self.premises[finding.id]
                        if link.get("relation") == "supports"
                    ]
                finding.verdict = worst(labels) if labels else "unchecked"
        left_open = self.mark_model_not_run()
        conclusions = self.trace()
        links = [*self.link_checks.values(), *self.extra_links]
        chain = [check for check in links if check.scope == "chain"]
        summary = {
            "links": {label: sum(check.label == label for check in chain) for label in LABELS},
            "links_total": len(chain),
            "context_links": len(links) - len(chain),
            "conclusions": {
                status: sum(item.status == status for item in conclusions)
                for status in ("holds", "weak", "undermined")
            },
            "records_verified": sum(
                check.frozen == "verified" for check in self.record_checks.values()
            ),
            "records_failed": sum(not check.usable for check in self.record_checks.values()),
            "broken_fragments": sum(not check.ok for check in self.fragment_checks.values()),
            "contract": contract,
            "model_checks": (
                {
                    "status": "ran",
                    **{
                        k: self.reasoner.describe()[k]
                        for k in ("model", "calls", "cached", "errors")
                    },
                }
                if self.reasoner.verifier is not None
                else {
                    "status": "not_run",
                    "reason": self.reasoner.not_run_reason,
                    "unchecked_links_a_model_check_could_decide": left_open,
                }
            ),
        }
        report = CheckReport(
            schema="traced.investigation.check/1",
            checked_at=self.now,
            checker={"version": CHECKER_VERSION, "reasoning": self.reasoner.describe()},
            bundle={
                "fingerprint": self.fingerprint,
                "case_id": self.case.get("id"),
                "title": self.case.get("title"),
                "question": self.case.get("question"),
                "contract": contract,
            },
            records=list(self.record_checks.values()),
            fragments=list(self.fragment_checks.values()),
            links=links,
            findings=list(self.finding_checks.values()),
            conclusions=conclusions,
            notes=self.notes,
            summary=summary,
        )
        report.checked_bundle = self.checked_bundle(summary)
        self.reasoner.save()
        return report


def check_bundle(
    data: Mapping[str, Any],
    *,
    store: RecordStore | None = None,
    reasoner: Reasoner | None = None,
    now: str | None = None,
    model_finding_ids: set[str] | None = None,
) -> CheckReport:
    """Check one bundle (as JSON data) and return a labelled report."""
    return _Checker(data, store, reasoner, now, model_finding_ids).run()
