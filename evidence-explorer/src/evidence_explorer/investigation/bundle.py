"""Add checkable draft findings to native saved evidence or Maltego records.

Native saved bundles retain their existing case, records and history. Maltego
records go through the existing saved-source projection (``build_source_import``
then ``build_source_bundle``), preserving the identity, hash, and provenance that
``./traced maltego case`` gives them. Both paths share the same draft projection:
claims and reasoning steps become ``inference`` findings, and assumptions become
``assumption`` findings.

Citations become exact text fragments. The model names a record label, a line
number, and a verbatim quote; offsets are located mechanically, never taken
from the model. A citation that does not resolve to an exact span is kept on
its step as a rejected citation, so the step stays visible and the checker
labels it unsupported instead of the builder silently repairing its meaning.

Arrows run premise → finding: a cited record ``supports`` a step, a step
``supports`` its claim, and an assumption is a ``depends_on`` premise of the
step or claim that relies on it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict

from evidence_explorer.collection.maltego.records import (
    MaltegoLookupRecord,
    build_source_import,
)
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.source_bundles import build_source_bundle

from . import INVESTIGATOR_VERSION
from .evidence import index_lines, line_for_offset, occurrences

INVESTIGATION_METHOD = "traced_investigator"


def digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftCitation(_Strict):
    record: str
    line: int
    quote: str


class DraftStep(_Strict):
    id: str
    statement: str
    citations: list[DraftCitation]
    assumption_ids: list[str]


class DraftClaim(_Strict):
    id: str
    statement: str
    steps: list[DraftStep]
    assumption_ids: list[str]


class DraftAssumption(_Strict):
    id: str
    statement: str
    reason: str


class InvestigationDraft(_Strict):
    """What the model writes: claims, steps with quotes, assumptions, gaps."""

    claims: list[DraftClaim]
    assumptions: list[DraftAssumption]
    gaps: list[str]


@dataclass(frozen=True)
class Resolution:
    start: int | None
    end: int | None
    how: str

    @property
    def ok(self) -> bool:
        return self.start is not None


def resolve_citation(text: str, quote: str, line_number: int | None) -> Resolution:
    """Find a verbatim quote in saved text, preferring the cited line."""
    if not quote or not quote.strip():
        return Resolution(None, None, "empty quote")
    lines = index_lines(text)
    attempts = [(quote, "")]
    if quote.strip() != quote:
        attempts.append((quote.strip(), " after trimming surrounding whitespace"))
    for candidate, note in attempts:
        if line_number is not None and 1 <= line_number <= len(lines):
            line = lines[line_number - 1]
            index = text.find(candidate, line.start, line.end)
            if index != -1:
                return Resolution(
                    index, index + len(candidate), f"exact quote on the cited line{note}"
                )
        found = occurrences(text, candidate)
        if not found:
            continue
        if line_number is not None and 1 <= line_number <= len(lines):
            target = lines[line_number - 1].start
            index = min(found, key=lambda position: (abs(position - target), position))
        else:
            index = found[0]
        actual = line_for_offset(lines, index).number
        detail = (
            f"exact quote found on line {actual}"
            + (f" (cited line {line_number})" if line_number is not None else "")
            + (f"; {len(found)} occurrences, nearest used" if len(found) > 1 else "")
        )
        return Resolution(index, index + len(candidate), detail + note)
    return Resolution(None, None, "quote not found in the saved record text")


@dataclass
class BuiltInvestigation:
    bundle: dict
    labels: dict[str, str]  # record label → bundle record ID
    citations: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def source_bundle(records: Sequence[MaltegoLookupRecord], *, title: str, question: str) -> dict:
    """The existing saved-source projection of entity records, as JSON."""
    imported = build_source_import(records, title=title[:300], question=question)
    return build_source_bundle(imported).model_dump(mode="json")


def record_labels(
    bundle: Mapping[str, Any], records: Sequence[MaltegoLookupRecord]
) -> dict[str, str]:
    """Stable R1…Rn labels in the order records were supplied."""
    by_native = {record["metadata"].get("native_id"): record["id"] for record in bundle["records"]}
    labels: dict[str, str] = {}
    for record in records:
        bundle_id = by_native.get(record.record_id)
        if bundle_id and bundle_id not in labels.values():
            labels[f"R{len(labels) + 1}"] = bundle_id
    return labels


def build_investigation_bundle(
    records: Sequence[MaltegoLookupRecord],
    draft: InvestigationDraft,
    *,
    goal: str,
    subject: Mapping[str, Any],
    created_at: str,
    configuration: Mapping[str, Any] | None = None,
    coverage: Mapping[str, Any] | None = None,
) -> BuiltInvestigation:
    """Combine the saved-source bundle with the draft's findings and links."""
    bundle = source_bundle(records, title=goal, question=goal)
    labels = record_labels(bundle, records)
    texts = {record["id"]: record["text"] for record in bundle["records"]}
    configuration = {
        "method": INVESTIGATION_METHOD,
        "goal": goal,
        "subject": dict(subject),
        "record_labels": {
            label: next(
                r["metadata"].get("native_id") for r in bundle["records"] if r["id"] == bundle_id
            )
            for label, bundle_id in labels.items()
        },
        **dict(configuration or {}),
    }
    manifest = sorted(labels.values())
    run_id = "investigation:run:" + digest(
        [bundle["case"]["id"], manifest, configuration, draft.model_dump(mode="json")]
    )
    lineage_base = digest([goal, dict(subject)])
    return _append_draft(
        bundle,
        draft,
        labels=labels,
        run_id=run_id,
        lineage_base=lineage_base,
        created_at=created_at,
        configuration=configuration,
        coverage=coverage,
        input_fingerprint=digest([[bundle_id, texts[bundle_id]] for bundle_id in manifest]),
    )


def build_saved_investigation_bundle(
    bundle: dict,
    draft: InvestigationDraft,
    *,
    goal: str,
    created_at: str,
    record_ids: Sequence[str],
    configuration: Mapping[str, Any] | None = None,
) -> BuiltInvestigation:
    """Append a distinct draft assessment over selected native saved records.

    The original case, records, metadata, findings, runs, scenarios and reviews
    are preserved. Selection supplies evidence, not confirmed planning scope or
    human approval. Every call creates a new run, even for the same draft, so a
    previous review cannot attach to a newly requested assessment by accident.
    """
    EvidenceBundle.model_validate(bundle)
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("State the investigation question before drafting.")
    if isinstance(record_ids, str | bytes) or not record_ids:
        raise ValueError("Select at least one saved text record for the draft.")
    selected = list(dict.fromkeys(record_ids))
    snapshot = copy.deepcopy(bundle)
    by_id = {record["id"]: record for record in snapshot["records"]}
    for record_id in selected:
        if record_id not in by_id:
            raise ValueError(f"Unknown selected saved record: {record_id}")
        text = by_id[record_id].get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Selected record has no saved text: {record_id}")
    labels = {f"R{index}": record_id for index, record_id in enumerate(selected, start=1)}
    # Pin the full selected records, including their text, hashes and provenance.
    # The fingerprint never depends on a later source-registry or database read.
    input_fingerprint = digest(
        {"case_id": snapshot["case"]["id"], "records": [by_id[key] for key in sorted(selected)]}
    )
    configuration = {
        **copy.deepcopy(dict(configuration or {})),
        "method": INVESTIGATION_METHOD,
        "goal": goal.strip(),
        "record_labels": labels.copy(),
        "record_label_basis": "record_id",
        "selected_record_ids": selected.copy(),
        "draft": True,
    }
    run_id = "investigation:run:" + uuid.uuid4().hex
    return _append_draft(
        snapshot,
        draft.model_copy(deep=True),
        labels=labels,
        run_id=run_id,
        lineage_base=digest([snapshot["case"]["id"], goal.strip()]),
        created_at=created_at,
        configuration=configuration,
        coverage=None,
        input_fingerprint=input_fingerprint,
        preserve_existing=True,
    )


def _append_draft(
    bundle: dict,
    draft: InvestigationDraft,
    *,
    labels: dict[str, str],
    run_id: str,
    lineage_base: str,
    created_at: str,
    configuration: Mapping[str, Any],
    coverage: Mapping[str, Any] | None,
    input_fingerprint: str,
    preserve_existing: bool = False,
) -> BuiltInvestigation:
    """Shared exact-citation projection for imported and saved-source drafts."""
    texts = {record["id"]: record.get("text") for record in bundle["records"]}
    manifest = sorted(labels.values())
    built = BuiltInvestigation(bundle, labels)
    findings: list[dict] = []
    fragments: dict[tuple[str, int, int], dict] = {}
    links: list[dict] = []

    def finding(label: str, kind: str, statement: str, details: dict) -> str:
        snapshot_id = "investigation:finding:" + digest([run_id, label, statement])
        findings.append(
            {
                "snapshot_id": snapshot_id,
                "lineage_id": f"investigation:{lineage_base[:24]}:{label}",
                "version": 1,
                "run_id": run_id,
                "kind": kind,
                "statement": statement.strip(),
                "details": {"label": label, **details},
            }
        )
        return snapshot_id

    def link(source: dict, target: str, relation: str, basis: str, fragment_ids=()) -> None:
        links.append(
            {
                "id": "investigation:link:"
                + digest([run_id, source, target, relation, list(fragment_ids)]),
                "source": source,
                "target": {"type": "finding", "id": target},
                "relation": relation,
                "fragment_ids": list(fragment_ids),
                "basis": basis,
                "run_id": run_id,
            }
        )

    assumption_ids: dict[str, str] = {}
    for assumption in draft.assumptions:
        if not assumption.statement.strip():
            built.problems.append(f"Assumption {assumption.id} has no statement and was dropped.")
            continue
        if assumption.id in assumption_ids:
            built.problems.append(f"Duplicate assumption ID {assumption.id}; the first is kept.")
            continue
        assumption_ids[assumption.id] = finding(
            assumption.id,
            "assumption",
            assumption.statement,
            {"role": "assumption", "reason": assumption.reason.strip()},
        )

    def depends(ids: Sequence[str], target: str, owner: str) -> None:
        for assumption_id in dict.fromkeys(ids):
            if assumption_id not in assumption_ids:
                built.problems.append(f"{owner} relies on unknown assumption {assumption_id}.")
                continue
            link(
                {"type": "finding", "id": assumption_ids[assumption_id]},
                target,
                "depends_on",
                f"{owner} relies on stated assumption {assumption_id}.",
            )

    seen_claims: set[str] = set()
    for claim in draft.claims:
        if not claim.statement.strip() or claim.id in seen_claims:
            built.problems.append(f"Claim {claim.id} is empty or duplicated and was dropped.")
            continue
        seen_claims.add(claim.id)
        claim_id = finding(claim.id, "inference", claim.statement, {"role": "claim"})
        depends(claim.assumption_ids, claim_id, f"Claim {claim.id}")
        if not claim.steps:
            built.problems.append(f"Claim {claim.id} has no reasoning steps.")
        for position, step in enumerate(claim.steps, start=1):
            step_label = step.id.strip() or f"S{position}"
            label = (
                step_label if step_label.startswith(f"{claim.id}.") else f"{claim.id}.{step_label}"
            )
            rejected = []
            accepted = []
            for citation in step.citations:
                record_id = labels.get(citation.record.strip())
                entry = {
                    "step": label,
                    "record": citation.record,
                    "line": citation.line,
                    "quote": citation.quote,
                }
                if record_id is None:
                    entry.update(accepted=False, how="unknown record label")
                    rejected.append(entry)
                    built.citations.append(entry)
                    continue
                resolution = resolve_citation(texts[record_id], citation.quote, citation.line)
                entry.update(accepted=resolution.ok, how=resolution.how, record_id=record_id)
                if resolution.ok:
                    entry.update(start=resolution.start, end=resolution.end)
                    accepted.append((record_id, resolution.start, resolution.end))
                else:
                    rejected.append(entry)
                built.citations.append(entry)
            step_id = finding(
                label,
                "inference",
                step.statement,
                {
                    "role": "step",
                    "claim": claim.id,
                    "rejected_citations": [
                        {key: item[key] for key in ("record", "line", "quote", "how")}
                        for item in rejected
                    ],
                },
            )
            for record_id, start, end in dict.fromkeys(accepted):
                key = (record_id, start, end)
                if key not in fragments:
                    fragments[key] = {
                        "id": "investigation:fragment:"
                        + digest(
                            [run_id, record_id, start, end]
                            if preserve_existing
                            else [record_id, start, end]
                        ),
                        "record_id": record_id,
                        "selector": {"kind": "text", "start": start, "end": end},
                        "quote": texts[record_id][start:end],
                    }
                link(
                    {"type": "record", "id": record_id},
                    step_id,
                    "supports",
                    f"Quoted by step {label}: characters {start}–{end} of the saved record text.",
                    [fragments[key]["id"]],
                )
            depends(step.assumption_ids, step_id, f"Step {label}")
            link(
                {"type": "finding", "id": step_id},
                claim_id,
                "supports",
                f"Reasoning step {label} toward claim {claim.id}.",
            )
    bundle.setdefault("runs", []).append(
        {
            "id": run_id,
            "input_fingerprint": input_fingerprint,
            "input_manifest": manifest,
            "method": INVESTIGATION_METHOD,
            "method_version": INVESTIGATOR_VERSION,
            "created_at": created_at,
            "full_inputs_available": True,
            "recomputation_capability": "dependency_review_only",
            "configuration": configuration,
            "coverage": {
                "records": len(manifest),
                "claims": len(seen_claims),
                "assumptions": len(assumption_ids),
                "gaps": [gap.strip() for gap in draft.gaps if gap.strip()],
                "rejected_citations": sum(not item["accepted"] for item in built.citations),
                **dict(coverage or {}),
            },
            "status": "completed",
        }
    )
    bundle.setdefault("findings", []).extend(findings)
    bundle.setdefault("fragments", []).extend(fragments.values())
    bundle.setdefault("links", []).extend(links)
    # The contract is the gate: every fragment quote must match its offsets exactly.
    validated = EvidenceBundle.model_validate(bundle)
    built.bundle = bundle if preserve_existing else validated.model_dump(mode="json")
    return built
