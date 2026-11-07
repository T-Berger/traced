"""Source-neutral evidence transport and reference validation for bundle v1.

Unknown context stays nullable. Additive producer fields survive round trips.
Text offsets index the saved Unicode string, never HTML or a fetched live page.
The models are mutable transport objects; persistence boundaries revalidate them
and enforce saved snapshot identity. This module performs no storage or artifact
I/O. Archive versions and planning sidecars belong to the package adapters.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    """Preserve producer fields and revalidate nested instances at explicit boundaries."""

    # These transport objects are mutable; recheck nested instances at boundaries.
    model_config = ConfigDict(extra="allow", revalidate_instances="always")


class Case(Contract):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    question: str
    synthetic: bool = False
    coverage: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)


class Record(Contract):
    id: str = Field(min_length=1)
    kind: str
    source_label: str
    text: str | None = None
    content_hash: str
    source_url: str | None = None
    artifact_ref: str | None = None
    acquired_at: str | None = None
    published_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def saved_content_is_consistent(self) -> Record:
        if self.text is None and self.artifact_ref is None:
            raise ValueError("Record requires saved text or a relative artifact reference")
        if self.text is not None:
            expected = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
            if expected != self.content_hash:
                raise ValueError(f"Content hash mismatch for {self.id}")
        if self.artifact_ref:
            path = PurePosixPath(self.artifact_ref)
            if path.is_absolute() or ".." in path.parts or "\\" in self.artifact_ref:
                raise ValueError("Artifact references must be safe relative paths")
        return self


class Selector(Contract):
    kind: Literal["text", "whole_record", "image"] = "text"
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)


class Fragment(Contract):
    id: str = Field(min_length=1)
    record_id: str
    selector: Selector
    quote: str | None = None


class Run(Contract):
    id: str = Field(min_length=1)
    input_fingerprint: str
    input_manifest: list[str]
    method: str
    method_version: str
    created_at: str
    full_inputs_available: bool = False
    recomputation_capability: str = "dependency_review_only"
    configuration: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)
    status: str = "completed"


class Finding(Contract):
    snapshot_id: str = Field(min_length=1)
    lineage_id: str
    version: int = Field(ge=1)
    run_id: str
    kind: Literal["observation", "inference", "assumption"]
    statement: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)


class ObjectRef(Contract):
    type: Literal["record", "fragment", "finding"]
    id: str


class Link(Contract):
    id: str
    source: ObjectRef
    target: ObjectRef
    relation: Literal[
        "supports",
        "contradicts",
        "depends_on",
        "same_content",
        "documented_repost",
        "similar_wording",
        "part_of",
    ]
    fragment_ids: list[str] = Field(default_factory=list)
    basis: str
    run_id: str | None = None


class Scenario(Contract):
    id: str
    base_run_id: str
    excluded_record_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    status: str = "dependency_preview"
    result_run_id: str | None = None


class Review(Contract):
    id: str
    finding_snapshot_id: str
    run_id: str
    scenario_id: str | None = None
    reviewer: str
    disposition: Literal["use", "qualify", "withhold"]
    reason: str = Field(min_length=1)
    evidence_refs: list[ObjectRef] = Field(default_factory=list)
    timestamp: str
    origin: str = "imported"


def _validate_dependency_graph(graph: dict[str, list[str]]) -> None:
    """Reject cycles without depending on Python's recursion limit."""
    incoming = dict.fromkeys(graph, 0)
    for children in graph.values():
        for child in children:
            incoming[child] += 1
    ready = deque(finding_id for finding_id, count in incoming.items() if count == 0)
    visited = 0
    while ready:
        finding_id = ready.popleft()
        visited += 1
        for child in graph[finding_id]:
            incoming[child] -= 1
            if incoming[child] == 0:
                ready.append(child)
    if visited != len(graph):
        raise ValueError("Cyclic finding dependency")


class EvidenceBundle(Contract):
    """Carry one case's evidence, analysis provenance, and attributed review history.

    Validation checks internal references, saved text hashes, and exact spans.
    It does not verify factual truth, inspect artifact bytes, or turn imported
    review history into a new local decision.
    """

    schema_version: str = "1.0"
    case: Case
    records: list[Record]
    fragments: list[Fragment]
    runs: list[Run]
    findings: list[Finding]
    links: list[Link] = Field(default_factory=list)
    reviews: list[Review] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> EvidenceBundle:
        if self.schema_version.split(".")[0] != "1":
            raise ValueError("Unsupported evidence bundle major version")
        records = {r.id: r for r in self.records}
        fragments = {f.id: f for f in self.fragments}
        runs = {r.id: r for r in self.runs}
        findings = {f.snapshot_id: f for f in self.findings}
        scenarios = {s.id: s for s in self.scenarios}
        for name, items, ids in [
            ("records", self.records, records),
            ("fragments", self.fragments, fragments),
            ("runs", self.runs, runs),
            ("findings", self.findings, findings),
            ("scenarios", self.scenarios, scenarios),
            ("links", self.links, {x.id for x in self.links}),
            ("reviews", self.reviews, {x.id for x in self.reviews}),
        ]:
            if len(items) != len(ids):
                raise ValueError(f"Duplicate IDs in {name}")
        typed = {"record": records, "fragment": fragments, "finding": findings}

        def check_ref(ref: ObjectRef) -> None:
            if ref.id not in typed[ref.type]:
                raise ValueError(f"Unresolved {ref.type} reference: {ref.id}")

        for fragment in self.fragments:
            if fragment.record_id not in records:
                raise ValueError(f"Unresolved record for fragment {fragment.id}")
            record = records[fragment.record_id]
            selector = fragment.selector
            if selector.kind == "text":
                if record.text is None or selector.start is None or selector.end is None:
                    raise ValueError("Text selector needs saved text and offsets")
                if not (0 <= selector.start < selector.end <= len(record.text)):
                    raise ValueError(f"Invalid text span: {fragment.id}")
                if record.text[selector.start : selector.end] != fragment.quote:
                    raise ValueError(f"Quotation mismatch: {fragment.id}")
            elif selector.kind == "image" and record.artifact_ref is None:
                raise ValueError("Image selector requires a saved artifact")
        for run in self.runs:
            if any(rid not in records for rid in run.input_manifest):
                raise ValueError(f"Run {run.id} has unresolved input records")
        for finding in self.findings:
            if finding.run_id not in runs:
                raise ValueError(f"Unknown run for finding {finding.snapshot_id}")
        graph: dict[str, list[str]] = {fid: [] for fid in findings}
        for link in self.links:
            check_ref(link.source)
            check_ref(link.target)
            if any(fid not in fragments for fid in link.fragment_ids):
                raise ValueError(f"Unresolved supporting fragment in {link.id}")
            if link.run_id and link.run_id not in runs:
                raise ValueError(f"Unknown run in {link.id}")
            if link.relation in {"supports", "depends_on"}:
                if link.target.type != "finding":
                    raise ValueError("Support/dependency arrows point from premise to finding")
                if link.source.type == "finding":
                    graph[link.source.id].append(link.target.id)
            if link.relation in {"same_content", "documented_repost", "similar_wording"}:
                if link.source.type != "record" or link.target.type != "record":
                    raise ValueError("Content relationships connect records")
        _validate_dependency_graph(graph)
        for scenario in self.scenarios:
            if scenario.base_run_id not in runs:
                raise ValueError("Scenario references missing baseline run")
            if scenario.result_run_id and scenario.result_run_id not in runs:
                raise ValueError("Scenario references missing result run")
            if any(rid not in records for rid in scenario.excluded_record_ids):
                raise ValueError("Scenario references missing excluded record")
        for review in self.reviews:
            if review.finding_snapshot_id not in findings:
                raise ValueError("Review references missing finding snapshot")
            if findings[review.finding_snapshot_id].run_id != review.run_id:
                raise ValueError("Review run does not match finding snapshot")
            if review.scenario_id and review.scenario_id not in scenarios:
                raise ValueError("Review references missing scenario")
            for ref in review.evidence_refs:
                check_ref(ref)
        return self

    def fingerprint(self) -> str:
        """Stable identity for version-pinned jobs, independent of JSON key order."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()
