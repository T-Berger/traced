"""Pure queries over serialized evidence bundles used by the review interface.

These helpers do not create components or access storage. Typed object references
must retain their kind: a record and a finding may have the same identifier.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .contracts import EvidenceBundle, Finding, Link

Bundle = dict[str, Any]


def bundle_data(view: Bundle) -> Bundle:
    return view.get("bundle", view)


def finding_id(finding: Bundle) -> str:
    return str(finding.get("snapshot_id", finding.get("id", "")))


def reference(ref: object) -> tuple[str, str]:
    if not isinstance(ref, dict):
        return "", str(ref or "")
    kind = str(ref.get("type", ""))
    if kind in {"claim", "finding_snapshot", "claim_snapshot"}:
        kind = "finding"
    return kind, str(ref.get("id", ""))


def evidence_ids(
    bundle: Bundle, selected_finding: str | None, *, include_premises: bool = True
) -> tuple[set[str], set[str]]:
    """Follow declared premises to exact records, preserving typed references.

    An inference may cite another finding rather than a record directly. Walking
    only those declared links makes its underlying evidence accessible too.
    """
    fragments = {str(fragment["id"]): fragment for fragment in bundle.get("fragments", [])}
    records: set[str] = set()
    selected_fragments: set[str] = set()
    pending = [selected_finding] if selected_finding else []
    visited = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        for link in bundle.get("links", []):
            target_kind, target = reference(link.get("target"))
            if target_kind != "finding" or target != current:
                continue
            kind, source = reference(link.get("source"))
            if (
                include_premises
                and kind == "finding"
                and link.get("relation") in {"supports", "depends_on"}
            ):
                pending.append(source)
            if kind == "record":
                records.add(source)
            fragment_ids = list(link.get("fragment_ids", []))
            if kind == "fragment":
                fragment_ids.append(source)
            for fragment_id in fragment_ids:
                fragment_id = str(fragment_id)
                if fragment_id in fragments:
                    selected_fragments.add(fragment_id)
                    records.add(str(fragments[fragment_id]["record_id"]))
    return records, selected_fragments


def source_for_finding(bundle: Bundle, selected_finding: str | None) -> str | None:
    """Prefer supplied media, then the first linked saved record in bundle order."""
    record_ids, _ = evidence_ids(bundle, selected_finding)
    media = next(
        (
            str(r["id"])
            for r in bundle.get("records", [])
            if str(r["id"]) in record_ids and r.get("artifact_ref")
        ),
        None,
    )
    if media:
        return media
    return next(
        (
            str(record["id"])
            for record in bundle.get("records", [])
            if str(record["id"]) in record_ids
        ),
        None,
    )


def initial_selection(bundle: Bundle) -> tuple[str | None, str | None]:
    """Prefer a source-linked inference without depending on demo fixture IDs."""
    findings = bundle.get("findings", [])
    inferences = [finding for finding in findings if finding.get("kind") == "inference"]
    preferred = next(
        (
            finding
            for finding in inferences
            if evidence_ids(bundle, finding_id(finding), include_premises=False)[0]
        ),
        None,
    )
    preferred = preferred or (inferences[0] if inferences else None)
    selected = finding_id(preferred or (findings[0] if findings else {})) or None
    return selected, source_for_finding(bundle, selected)


class EvidenceIndex:
    """Request-scoped indexes referencing existing contracts, never copied source text."""

    def __init__(self, bundle: EvidenceBundle, findings: Sequence[Finding] | None = None):
        self.bundle = bundle
        self.records = {record.id: record for record in bundle.records}
        self.fragments = {fragment.id: fragment for fragment in bundle.fragments}
        self.findings = {
            finding.snapshot_id: finding
            for finding in (findings if findings is not None else bundle.findings)
        }
        self._links: dict[str, list[Link]] = {}
        for link in bundle.links:
            if link.target.type == "finding":
                self._links.setdefault(link.target.id, []).append(link)

    def links_to(self, selected: str | None) -> Sequence[Link]:
        return self._links.get(selected, ())

    def evidence_for(self, selected: str | None) -> tuple[set[str], set[str]]:
        records: set[str] = set()
        fragments: set[str] = set()
        for link in self.links_to(selected):
            if link.source.type == "record":
                records.add(link.source.id)
            fragment_ids = list(link.fragment_ids)
            if link.source.type == "fragment":
                fragment_ids.append(link.source.id)
            for fid in fragment_ids:
                fragment = self.fragments.get(fid)
                if fragment is not None:
                    fragments.add(fid)
                    records.add(fragment.record_id)
        return records, fragments

    def first_source(self, selected: str | None) -> str | None:
        record_ids, _ = self.evidence_for(selected)
        return next((rid for rid in self.records if rid in record_ids), None)

    def initial(self) -> tuple[str | None, str | None]:
        inferences = [finding for finding in self.findings.values() if finding.kind == "inference"]
        preferred = next(
            (finding for finding in inferences if self.first_source(finding.snapshot_id)), None
        )
        preferred = preferred or (inferences[0] if inferences else None)
        preferred = preferred or next(iter(self.findings.values()), None)
        fid = preferred.snapshot_id if preferred else None
        return fid, self.first_source(fid)
