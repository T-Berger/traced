"""Deterministic text checks shared by the investigator and the checker.

Pure and offline: line indexing, exact quote location, typed-value extraction,
and property concepts for ``property: value`` lines (the rendering Maltego uses
for entity results, and the shape of most structured OSINT records).

These heuristics only find problems or propose candidate lines. A value found
in a quote shows that the quote mentions it; it never shows a statement is true.
Free-form names are used only to check that a statement's names appear in its
evidence, never to declare a conflict: two spellings of one organisation are
common, so name disagreements are left to the reasoning check and to people.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Literal

ValueKind = Literal[
    "email", "url", "ipv4", "domain", "date", "month", "year", "number", "quoted", "name"
]
DATE_KINDS = frozenset({"date", "month", "year"})

# Property concepts. A statement word or a record key maps to every concept it can
# name ("registered" can mean creation, registrar, or registrant), so a check only
# treats a value as out of role when no concept is shared at all.
CONCEPT_WORDS: dict[str, tuple[str, ...]] = {
    "creation": (
        "created",
        "creation",
        "create",
        "creates",
        "registered",
        "registration",
        "incorporated",
        "incorporation",
        "founded",
        "founding",
        "established",
        "establishment",
        "formed",
        "formation",
        "inception",
        "since",
        "born",
        "birth",
    ),
    "updated": (
        "updated",
        "update",
        "updates",
        "changed",
        "change",
        "changes",
        "modified",
        "modification",
        "renewed",
        "renewal",
    ),
    "expiry": ("expires", "expire", "expired", "expiry", "expiration", "expiring"),
    "registrar": (
        "registrar",
        "registrars",
        "registered",
        "registration",
        "reseller",
        "sponsoring",
    ),
    "registrant": (
        "registrant",
        "registrants",
        "registered",
        "holder",
        "owner",
        "owners",
        "owned",
        "owns",
        "ownership",
    ),
    "operator": (
        "operates",
        "operate",
        "operated",
        "operator",
        "operators",
        "operating",
        "runs",
        "running",
        "behind",
        "controls",
        "control",
        "controlled",
        "controller",
        "manages",
        "manage",
        "managed",
        "maintains",
        "maintained",
    ),
    "dns": ("nameserver", "nameservers", "ns", "dns", "mx", "resolver", "zone"),
    "hosting": (
        "hosted",
        "hosting",
        "host",
        "hosts",
        "hoster",
        "provider",
        "isp",
        "asn",
        "autonomous",
        "netblock",
        "network",
        "datacenter",
    ),
    "location": (
        "country",
        "countries",
        "located",
        "location",
        "based",
        "headquartered",
        "headquarters",
        "hq",
        "jurisdiction",
        "city",
        "address",
        "region",
        "province",
        "nation",
        "residence",
        "resides",
        "lives",
        "street",
        "postal",
        "zip",
    ),
    "contact": (
        "email",
        "mail",
        "contact",
        "contacts",
        "phone",
        "telephone",
        "tel",
        "fax",
        "mobile",
    ),
    "officer": (
        "officer",
        "officers",
        "director",
        "directors",
        "ceo",
        "founder",
        "founders",
        "employee",
        "employees",
        "staff",
        "president",
        "shareholder",
        "shareholders",
        "partner",
        "member",
        "members",
    ),
    # Generic nouns: they say what kind of thing, not which role or property.
    "entity": (
        "organization",
        "organisation",
        "organizations",
        "organisations",
        "org",
        "company",
        "companies",
        "corporation",
        "firm",
        "employer",
        "business",
        "person",
        "people",
        "individual",
        "entity",
    ),
    "status": (
        "status",
        "active",
        "inactive",
        "dissolved",
        "suspended",
        "prohibited",
        "locked",
    ),
    "ip": ("ip", "ips", "ipv4", "ipv6", "resolves", "resolved", "resolution"),
}
GENERIC_CONCEPTS = frozenset({"entity"})
WORD_CONCEPTS: dict[str, frozenset[str]] = {}
for _concept, _words in CONCEPT_WORDS.items():
    for _word in _words:
        WORD_CONCEPTS[_word] = WORD_CONCEPTS.get(_word, frozenset()) | {_concept}

_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_YEAR = r"(?:19|20)\d{2}"

_URL = re.compile(r"\bhttps?://[^\s\"'<>()\[\]{}]+", re.IGNORECASE)
_EMAIL = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}\b"
)
_IPV4 = re.compile(
    r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
    r"(?![\d.]\d)"
)
_ISO_DATE = re.compile(rf"(?<!\d)({_YEAR})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(?!\d)")
_DOT_DATE = re.compile(rf"(?<![\d.])(0?[1-9]|[12]\d|3[01])\.(0?[1-9]|1[0-2])\.({_YEAR})(?!\d)")
_DMY = re.compile(
    rf"\b(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?\s+({_MONTH})\.?,?\s+({_YEAR})\b", re.IGNORECASE
)
_MDY = re.compile(
    rf"\b({_MONTH})\.?\s+(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?,?\s+({_YEAR})\b", re.IGNORECASE
)
_MY = re.compile(rf"\b({_MONTH})\.?\s+(?:of\s+)?({_YEAR})\b", re.IGNORECASE)
_DOMAIN = re.compile(
    r"(?<![\w@.-])((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24})"
    r"(?![\w-]|\.[A-Za-z0-9])"
)
_FILE_LIKE = re.compile(r"^[\w-]+\.(?:py|json|txt|html|csv|js|ts|yaml|yml|log|pdf|png|jpg|md)$")
_LONE_YEAR = re.compile(rf"(?<![\w.:/-])({_YEAR})(?![\w-]|\.\d|:\d)")
_NUMBER = re.compile(r"(?<![\w.:/+-])(\d{2,}(?:[.,]\d{3})*(?:\.\d+)?)(?![\w:/-]|\.\d)")
_QUOTED = re.compile(r"\"([^\"\n]{2,200})\"|“([^”\n]{2,200})”|`([^`\n]{2,200})`")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9&'’.\-]*[A-Za-z0-9]|[A-Za-z0-9]")
_KEY = re.compile(r"^\s*([A-Za-z][\w .\-]{0,63}?):(?:\s|$)")
_CLAUSE_BREAK = re.compile(
    r";|\.(?:\s|$)|\s(?:and|but|while|whereas|although|however)\s|\n", re.IGNORECASE
)
_TOKEN = re.compile(r"[a-z0-9]+")
# Record, claim, step, and assumption labels (R1, C2, C2.S1, A3) are not names.
_LABEL = re.compile(r"[RCSAO]\d+(?:\.[A-Z]?\d+)*")

LEGAL_SUFFIXES = frozenset(
    "llc inc incorporated ltd limited gmbh ag sa sas sarl bv nv plc corp corporation co "
    "company kg ug oy ab as spa srl pty lp llp pte se".split()
)
CONNECTORS = frozenset("of and & de der van von la le du del da di for".split())
# Capitalised words that start sentences or name methods, not entities.
STOP_CAPITALIZED = frozenset(
    """
    a an the this that these those it its there their both each all no not none any some
    one two three according based because since as at by for from in into on of or and but
    if so to with without while when where which who whom whose what why how is are was were
    be been has have had do does did can could may might must should would will however
    therefore thus also then than only further furthermore moreover although whereas yet
    record records entity entities domain domains whois rdap dns ip ipv4 ipv6 asn utc gmt
    url urls id ids iana icann tld mx ns api json osint data mcp lookup lookups step steps
    claim claims assumption assumptions evidence registrar registrant note notes line lines
    result results source sources value values date dates email name names organization
    organisation company person people website websites site sites server servers status we
    they he she you our my your his her them us yes either neither many most several few
    none nothing given unknown possibly likely probably
    """.split()
)
CONTENT_STOPWORDS = STOP_CAPITALIZED | frozenset(
    "about after before during over under between within also used uses using shows show "
    "shown states state stated listed lists list reported reports report returned returns "
    "record records field fields entry entries".split()
)


@dataclass(frozen=True)
class Value:
    kind: ValueKind
    text: str
    norm: str
    start: int
    end: int


def specific(concepts: frozenset[str]) -> frozenset[str]:
    """Role and property concepts, without generic nouns."""
    return concepts - GENERIC_CONCEPTS


@dataclass(frozen=True)
class Line:
    number: int  # 1-based
    start: int
    end: int  # exclusive; the newline is not part of the line
    text: str
    key: str | None

    @cached_property
    def concepts(self) -> frozenset[str]:
        return key_concepts(self.key) if self.key else frozenset()

    @cached_property
    def values(self) -> tuple[Value, ...]:
        return extract_values(self.text)

    @cached_property
    def index(self) -> TextIndex:
        return index_text(self.text)


def index_lines(text: str) -> tuple[Line, ...]:
    lines: list[Line] = []
    position = 0
    for number, raw in enumerate(text.split("\n"), start=1):
        match = _KEY.match(raw)
        key = match.group(1).strip() if match else None
        lines.append(Line(number, position, position + len(raw), raw, key))
        position += len(raw) + 1
    return tuple(lines)


def line_for_offset(lines: Sequence[Line], offset: int) -> Line:
    starts = [line.start for line in lines]
    return lines[max(0, bisect.bisect_right(starts, offset) - 1)]


def lines_for_span(lines: Sequence[Line], start: int, end: int) -> list[Line]:
    """Every line that a character span touches."""
    return [line for line in lines if line.start <= max(start, end - 1) and line.end >= start]


def occurrences(text: str, quote: str, limit: int = 50) -> list[int]:
    if not quote:
        return []
    found: list[int] = []
    index = text.find(quote)
    while index != -1 and len(found) < limit:
        found.append(index)
        index = text.find(quote, index + 1)
    return found


def split_words(text: str) -> list[str]:
    """Lower-case word tokens; camelCase and snake_case keys split into words."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    return _TOKEN.findall(spaced.casefold())


def key_concepts(key: str) -> frozenset[str]:
    concepts: frozenset[str] = frozenset()
    for word in split_words(key):
        concepts |= WORD_CONCEPTS.get(word, frozenset())
    return concepts


def concept_words(text: str) -> dict[str, frozenset[str]]:
    """Concept-bearing words of a statement and the concepts each can name."""
    return {
        word: WORD_CONCEPTS[word]
        for word in _TOKEN.findall(text.casefold())
        if word in WORD_CONCEPTS
    }


def _mask(chars: list[str], start: int, end: int) -> None:
    # A non-space marker keeps names on either side of a value from merging.
    for index in range(start, end):
        chars[index] = "\x00"


def _name_values(masked: str) -> list[Value]:
    tokens = [(m.group(0), m.start(), m.end()) for m in _WORD.finditer(masked)]
    values: list[Value] = []
    run: list[tuple[str, int, int]] = []

    def capitalised(word: str) -> bool:
        return word[0].isupper() or (len(word) > 1 and word.isupper())

    def flush() -> None:
        while run and run[-1][0].casefold() in CONNECTORS:
            run.pop()
        while run and run[0][0].casefold() in STOP_CAPITALIZED | CONNECTORS:
            run.pop(0)
        if not run:
            return
        words = [word for word, _, _ in run]
        significant = [
            part
            for word in words
            for part in _TOKEN.findall(word.casefold())
            if len(part) > 1
            and part not in LEGAL_SUFFIXES
            and part not in CONNECTORS
            and part not in STOP_CAPITALIZED
        ]
        start, end = run[0][1], run[-1][2]
        sentence_start = start == 0 or bool(re.search(r"[.!?:]\s*$", masked[:start]))
        single_plain = len(words) == 1 and not any(c.isupper() for c in words[0][1:])
        if significant and not (sentence_start and single_plain):
            values.append(Value("name", masked[start:end], " ".join(significant), start, end))

    previous_end = None
    for word, start, end in tokens:
        gap = masked[previous_end:start] if previous_end is not None else ""
        joined = previous_end is not None and gap.strip() == ""
        if _LABEL.fullmatch(word):
            flush()
            run.clear()
        elif capitalised(word) and word.casefold() not in STOP_CAPITALIZED - {"the"}:
            if run and not joined:
                flush()
                run.clear()
            run.append((word, start, end))
        elif run and joined and word.casefold() in CONNECTORS:
            run.append((word, start, end))
        else:
            flush()
            run.clear()
        previous_end = end
    flush()
    return values


def extract_values(text: str) -> tuple[Value, ...]:
    """Typed values in reading order; each character belongs to at most one value."""
    chars = list(text)
    found: list[Value] = []

    def take(pattern: re.Pattern[str], kind: ValueKind, normalise) -> None:
        current = "".join(chars)
        for match in pattern.finditer(current):
            norm = normalise(match)
            if norm is None:
                continue
            found.append(
                Value(kind, text[match.start() : match.end()], norm, match.start(), match.end())
            )
            _mask(chars, match.start(), match.end())

    take(_URL, "url", lambda m: m.group(0).rstrip(".,;:").casefold())
    take(_EMAIL, "email", lambda m: m.group(0).casefold())
    take(_IPV4, "ipv4", lambda m: m.group(0))
    take(_ISO_DATE, "date", lambda m: f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
    take(_DOT_DATE, "date", lambda m: f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}")
    take(
        _DMY,
        "date",
        lambda m: f"{m.group(3)}-{_MONTHS[m.group(2).casefold()]:02d}-{int(m.group(1)):02d}",
    )
    take(
        _MDY,
        "date",
        lambda m: f"{m.group(3)}-{_MONTHS[m.group(1).casefold()]:02d}-{int(m.group(2)):02d}",
    )
    take(_MY, "month", lambda m: f"{m.group(2)}-{_MONTHS[m.group(1).casefold()]:02d}")
    take(
        _DOMAIN,
        "domain",
        lambda m: (
            None if _FILE_LIKE.match(m.group(1).casefold()) else m.group(1).casefold().rstrip(".")
        ),
    )
    take(_LONE_YEAR, "year", lambda m: m.group(1))
    take(_NUMBER, "number", lambda m: re.sub(r"[.,]", "", m.group(1)))
    for match in _QUOTED.finditer(text):
        inner = next(group for group in match.groups() if group is not None)
        norm = " ".join(inner.casefold().split())
        if len(norm) >= 2:
            found.append(Value("quoted", inner, norm, match.start(), match.end()))
    found.extend(_name_values("".join(chars)))
    unique: dict[tuple[str, str], Value] = {}
    for value in sorted(found, key=lambda v: (v.start, v.kind)):
        unique.setdefault((value.kind, value.norm), value)
    return tuple(unique.values())


@dataclass(frozen=True)
class TextIndex:
    """What a piece of evidence text contains, for presence checks."""

    folded: str
    tokens: frozenset[str]
    dates: frozenset[str]
    months: frozenset[str]
    years: frozenset[str]
    numbers: frozenset[str]


def index_text(text: str) -> TextIndex:
    values = extract_values(text)
    dates = {v.norm for v in values if v.kind == "date"}
    months = {v.norm for v in values if v.kind == "month"} | {d[:7] for d in dates}
    years = {v.norm for v in values if v.kind == "year"} | {m[:4] for m in months}
    tokens = frozenset(token for token in _TOKEN.findall(text.casefold()) if len(token) > 1)
    years |= {token for token in tokens if re.fullmatch(_YEAR, token)}
    numbers = {v.norm for v in values if v.kind == "number"} | {
        token for token in tokens if token.isdigit()
    }
    return TextIndex(
        " ".join(text.casefold().split()),
        tokens,
        frozenset(dates),
        frozenset(months),
        frozenset(years),
        frozenset(numbers),
    )


def merge_indexes(indexes: Iterable[TextIndex]) -> TextIndex:
    items = list(indexes)
    return TextIndex(
        "\n".join(item.folded for item in items),
        frozenset().union(*(item.tokens for item in items)),
        frozenset().union(*(item.dates for item in items)),
        frozenset().union(*(item.months for item in items)),
        frozenset().union(*(item.years for item in items)),
        frozenset().union(*(item.numbers for item in items)),
    )


def _bounded_in(needle: str, haystack: str) -> bool:
    pattern = rf"(?<![a-z0-9\-]){re.escape(needle)}(?![a-z0-9\-])"
    return re.search(pattern, haystack) is not None


def contains(index: TextIndex, value: Value) -> bool:
    if value.kind in {"email", "url", "ipv4", "domain"}:
        return _bounded_in(value.norm, index.folded)
    if value.kind == "date":
        return value.norm in index.dates
    if value.kind == "month":
        return value.norm in index.months
    if value.kind == "year":
        return value.norm in index.years
    if value.kind == "number":
        return value.norm in index.numbers
    if value.kind == "quoted":
        return value.norm in index.folded
    return all(part in index.tokens for part in value.norm.split())


def _date_level(value: Value) -> int:
    return {"year": 4, "month": 7, "date": 10}[value.kind]


@dataclass(frozen=True)
class Clause:
    text: str
    concepts: frozenset[str]
    values: tuple[Value, ...]


@dataclass(frozen=True)
class Profile:
    """A statement's checkable content, with values the case already gives removed."""

    text: str
    values: tuple[Value, ...]
    words: dict[str, frozenset[str]]
    clauses: tuple[Clause, ...]

    @property
    def concepts(self) -> frozenset[str]:
        return frozenset().union(*self.words.values()) if self.words else frozenset()

    @property
    def roles(self) -> frozenset[str]:
        return specific(self.concepts)

    @property
    def checkable(self) -> bool:
        return bool(self.values or self.words)

    @property
    def content_tokens(self) -> frozenset[str]:
        return frozenset(
            token
            for token in _TOKEN.findall(self.text.casefold())
            if len(token) >= 4 and token not in CONTENT_STOPWORDS
        )


def given_index(texts: Iterable[str]) -> TextIndex:
    return merge_indexes(index_text(text) for text in texts if text)


_MASKED_KINDS = frozenset({"name", "domain", "email", "url"})


def _outside_values(text: str, values: Iterable[Value]) -> str:
    """The text with names, domains, emails, and URLs blanked: their words are not properties."""
    chars = list(text)
    for value in values:
        if value.kind in _MASKED_KINDS:
            chars[value.start : value.end] = " " * (value.end - value.start)
    return "".join(chars)


def profile(text: str, given: TextIndex | None = None) -> Profile:
    def kept(value: Value) -> bool:
        return given is None or not contains(given, value)

    found = extract_values(text)
    values = tuple(v for v in found if kept(v))
    clauses = []
    position = 0
    for match in [*_CLAUSE_BREAK.finditer(text), None]:
        end = match.start() if match else len(text)
        part = text[position:end]
        if part.strip():
            part_values = extract_values(part)
            words = concept_words(_outside_values(part, part_values))
            concepts = frozenset().union(*words.values()) if words else frozenset()
            clause_values = tuple(v for v in part_values if kept(v))
            clauses.append(Clause(part.strip(), concepts, clause_values))
        position = match.end() if match else len(text)
    return Profile(text, values, concept_words(_outside_values(text, found)), tuple(clauses))


def missing_values(statement: Profile, index: TextIndex) -> list[Value]:
    return [value for value in statement.values if not contains(index, value)]


def role_mismatches(statement: Profile, lines: Iterable[Line]) -> list[tuple[Value, list[Line]]]:
    """Values that appear only under keys naming none of the statement's concepts.

    Lines without a recognised key (free text, JSON blobs) are neutral: they
    neither confirm nor rule out a role.
    """
    if not statement.roles:
        return []
    candidates = list(lines)
    mismatches = []
    for value in statement.values:
        if value.kind in DATE_KINDS | {"number", "quoted"}:
            continue
        holding = [line for line in candidates if contains(line.index, value)]
        known = [line for line in holding if specific(line.concepts)]
        if known and all(not (specific(line.concepts) & statement.roles) for line in known):
            mismatches.append((value, known))
    return mismatches


def uncovered_words(statement: Profile, lines: Iterable[Line]) -> list[str]:
    """Concept words of the statement that no line's key can name."""
    shown = frozenset().union(*(line.concepts for line in lines), frozenset())
    return sorted(
        word
        for word, concepts in statement.words.items()
        if specific(concepts) and not specific(concepts) & shown
    )


# The properties under which a kind of value can contradict a statement. A street
# line that happens to mention a domain says nothing about a registrar's domain.
NATURAL_CONCEPTS: dict[str, frozenset[str]] = {
    "date": frozenset({"creation", "updated", "expiry"}),
    "email": frozenset({"contact"}),
    "ipv4": frozenset({"ip", "dns", "hosting"}),
    "domain": frozenset({"dns", "registrar", "hosting"}),
}


def _group(kind: str) -> str | None:
    if kind in DATE_KINDS:
        return "date"
    return kind if kind in NATURAL_CONCEPTS else None


def relation(group: str, said: Sequence[Value], shown: Sequence[Value]) -> str | None:
    """``agree``, ``disagree``, or None when the values cannot be compared."""
    if not said or not shown:
        return None
    if group == "date":
        pairs = [(s, v) for s in said for v in shown if _date_level(v) >= _date_level(s)]
        if not pairs:
            return None
        return "agree" if any(s.norm == v.norm[: _date_level(s)] for s, v in pairs) else "disagree"
    stated, listed = {v.norm for v in said}, {v.norm for v in shown}
    if group == "domain":
        related = any(
            a == b or a.endswith("." + b) or b.endswith("." + a) for a in stated for b in listed
        )
    else:
        related = bool(stated & listed)
    return "agree" if related else "disagree"


@dataclass(frozen=True)
class Comparison:
    """How every line naming one clause's property compares with that clause."""

    clause: Clause
    group: str
    said: tuple[str, ...]
    agree: tuple[tuple[object, Line], ...]
    disagree: tuple[tuple[object, Line, tuple[str, ...]], ...]


def compare_lines(statement: Profile, lines: Sequence[tuple[object, Line]]) -> list[Comparison]:
    """Compare each clause's typed values with lines that name the same property.

    Lines carry a caller tag (for example a record ID and line number). Only value
    kinds natural to the shared property are compared: dates under creation,
    update, or expiry; domains under DNS, registrar, or hosting; and so on.
    """
    results = []
    for clause in statement.clauses:
        roles = specific(clause.concepts)
        for group, natural in NATURAL_CONCEPTS.items():
            said = [v for v in clause.values if _group(v.kind) == group]
            shared = roles & natural
            if not said or not shared:
                continue
            agree, disagree = [], []
            for tag, line in lines:
                if not specific(line.concepts) & shared:
                    continue
                shown = [v for v in line.values if _group(v.kind) == group]
                outcome = relation(group, said, shown)
                if outcome == "agree":
                    agree.append((tag, line))
                elif outcome == "disagree":
                    disagree.append((tag, line, tuple(sorted({v.norm for v in shown}))))
            if agree or disagree:
                results.append(
                    Comparison(
                        clause,
                        group,
                        tuple(sorted({v.norm for v in said})),
                        tuple(agree),
                        tuple(disagree),
                    )
                )
    return results


def conflicting_lines(
    statement: Profile, lines: Iterable[Line]
) -> list[tuple[Line, list[dict], Clause]]:
    """Every line that disagrees with a clause on a property they share (no consensus rule)."""
    found = []
    for comparison in compare_lines(statement, [(None, line) for line in lines]):
        for _, line, shown in comparison.disagree:
            detail = {"kind": comparison.group, "said": list(comparison.said), "shown": list(shown)}
            found.append((line, [detail], comparison.clause))
    return found


def relevance(statement: Profile, line: Line) -> int:
    """How strongly a line relates to a statement: values, then concepts, then words."""
    index = line.index
    score = 4 * sum(contains(index, value) for value in statement.values)
    score += 2 * len(statement.concepts & line.concepts)
    words = statement.content_tokens & index.tokens
    return score + min(len(words), 3)


def window(text: str, start: int, end: int, limit: int) -> tuple[int, int]:
    """A span of at most ``limit`` characters around [start, end) inside ``text``."""
    if end - start >= limit:
        return start, start + limit
    spare = limit - (end - start)
    left = max(0, start - spare // 2)
    right = min(len(text), left + limit)
    left = max(0, right - limit)
    return left, right
