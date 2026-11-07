"""Read-only source cards with declared evidence roles and separate source relationships."""

from __future__ import annotations

from collections import defaultdict
from urllib.parse import urlsplit

from .graph import direct_connections
from .selectors import evidence_ids, reference
from .source_browser import MAX_BROWSER_ITEMS, source_browser_data

_STANCE_LABELS = {"supports": "Supports", "contradicts": "Challenges", "context": "Context"}


def _role(link: dict) -> tuple[str, str]:
    stance = link.get("assessment_citation_stance")
    if stance in _STANCE_LABELS:
        return stance, _STANCE_LABELS[stance] + " · model proposal"
    relation = link.get("relation")
    if relation in {"supports", "contradicts"}:
        return relation, _STANCE_LABELS[relation]
    if relation == "depends_on":
        return "input", "Assessment input" if link.get(
            "assessment_input"
        ) is True else "Input dependency"
    return "other", "Declared " + str(relation or "relationship").replace("_", " ")


def _passage(fragment_id: str, fragments: dict, record: dict) -> dict:
    fragment = fragments.get(fragment_id)
    if not fragment or fragment.get("record_id") != record["id"]:
        return {"id": fragment_id, "quote": None, "selector": {}, "exact": False}
    selector = fragment.get("selector") or {}
    quote = fragment.get("quote")
    text = record.get("text")
    start, end = selector.get("start"), selector.get("end")
    exact = (
        selector.get("kind") == "text"
        and isinstance(text, str)
        and isinstance(quote, str)
        and type(start) is int
        and type(end) is int
        and 0 <= start < end <= len(text)
        and text[start:end] == quote
    )
    return {"id": fragment_id, "quote": quote, "selector": selector, "exact": exact}


def _relationships(bundle: dict, records: dict) -> dict[str, list[dict]]:
    """Describe saved overlap and explicit links; never infer independent corroboration."""
    result = defaultdict(list)
    browser = source_browser_data(bundle, limit=MAX_BROWSER_ITEMS)
    for key, label in (("exact_groups", "Same saved text"), ("url_groups", "Shared supplied URL")):
        for group in browser[key]:
            members = [rid for rid in group["record_ids"] if rid in records]
            for rid in members:
                result[rid].append(
                    {
                        "label": label,
                        "basis": group["basis"],
                        "url": group.get("url"),
                        "peers": [records[peer] for peer in members if peer != rid],
                        "peer_count": group["count"] - 1,
                    }
                )
    for link in bundle.get("links", []):
        source_kind, source = reference(link.get("source"))
        target_kind, target = reference(link.get("target"))
        if source_kind != "record" or target_kind != "record":
            continue
        if source not in records or target not in records:
            continue
        for rid, peer in ((source, target), (target, source)):
            result[rid].append(
                {
                    "label": "Declared "
                    + str(link.get("relation", "relationship")).replace("_", " "),
                    "basis": link.get("basis") or "No relationship basis supplied.",
                    "peers": [records[peer]],
                    "peer_count": 1,
                    "direction": (
                        f"{records[source]['source_label']} → {records[target]['source_label']}"
                    ),
                    "link_id": link["id"],
                }
            )
    return result


def evidence_source_cards(bundle: dict, selected: str | None) -> list[dict]:
    """Retain multiple direct roles and label premise-only evidence without promoting it."""
    ids, _ = evidence_ids(bundle, selected)
    direct, _ = evidence_ids(bundle, selected, include_premises=False)
    records = {record["id"]: record for record in bundle.get("records", [])}
    fragments = {fragment["id"]: fragment for fragment in bundle.get("fragments", [])}
    links = {link["id"]: link for link in bundle.get("links", [])}
    finding = next(
        (item for item in bundle.get("findings", []) if item.get("snapshot_id") == selected), {}
    )
    assessment = finding.get("details", {}).get("assessment") or {}
    challenge = (assessment.get("metadata") or {}).get("challenge") or {}
    challenged = {
        fid
        for objection in challenge.get("findings", [])
        for fid in objection.get("fragment_ids", [])
    }
    relationships = _relationships(bundle, records)
    connections = defaultdict(list)
    for edge in direct_connections(bundle, selected):
        if edge["source_kind"] == "record":
            connections[edge["source_ref"]].extend(links[lid] for lid in edge["link_ids"])
    ordered = [record for rid, record in records.items() if rid in ids]
    ordered.sort(
        key=lambda record: (record["id"] not in direct, not bool(record.get("artifact_ref")))
    )
    cards = []
    for record in ordered:
        roles = {}
        for link in connections[record["id"]]:
            fragment_ids = list(link.get("fragment_ids", []))
            source_kind, source = reference(link.get("source"))
            if source_kind == "fragment":
                fragment_ids.append(source)
            keys = [_role(link)]
            if link.get("challenge_input") is True or challenged.intersection(fragment_ids):
                keys.append(("challenge_input", "Automated challenge input"))
            for key in keys:
                role = roles.setdefault(
                    key, {"kind": key[0], "label": key[1], "links": [], "bases": [], "passages": {}}
                )
                role["links"].append(link["id"])
                basis = (
                    "Saved passage used by the automated challenge; this does not declare "
                    "that it contradicts the claim."
                    if key[0] == "challenge_input"
                    else link.get("basis")
                )
                if basis and basis not in role["bases"]:
                    role["bases"].append(basis)
                for fragment_id in fragment_ids:
                    role["passages"][fragment_id] = _passage(fragment_id, fragments, record)
        ordered_roles = sorted(
            roles.values(), key=lambda role: (role["kind"] == "input", role["label"])
        )
        for role in ordered_roles:
            role["passages"] = list(role["passages"].values())
        if not ordered_roles:
            ordered_roles = [
                {
                    "kind": "indirect",
                    "label": "Via linked finding",
                    "links": [],
                    "bases": [],
                    "passages": [],
                }
            ]
        try:
            domain = urlsplit(str(record.get("source_url") or "")).hostname
        except ValueError:
            domain = None
        cards.append(
            {
                "record": record,
                "direct": record["id"] in direct,
                "publisher": record.get("metadata", {}).get("publisher"),
                "domain": domain,
                "roles": ordered_roles,
                "relationships": relationships[record["id"]],
            }
        )
    return cards
