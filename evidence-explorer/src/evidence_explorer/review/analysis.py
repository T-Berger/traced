"""Evidence review operations over immutable bundle snapshots.

Dependency impact is a review preview, not a truth verdict. Duplicate text and
documented repost lineage are deliberately different relationships.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict, deque

from pydantic import ValidationError

from .contracts import EvidenceBundle
from .models import Impact, SourceGroup
from .verifier import (
    PROMPT_VERSION,
    RETRIEVAL_VERSION,
    CheckError,
    CheckOutput,
    OllamaVerifier,
    OpenRouterVerifier,
    Passage,
    Verifier,
)

SUPPORT_RELATIONS = {"supports", "depends_on"}
MAX_RECORDS = 12
MAX_FRAGMENTS = 20
MAX_FRAGMENT_BYTES = 1800
MAX_PASSAGE_BYTES = 18000
MAX_STATEMENT_BYTES = 4000


def _excluded(bundle: EvidenceBundle, values: list[str] | None) -> set[str]:
    excluded = set(values or [])
    unknown = excluded - {record.id for record in bundle.records}
    if unknown:
        raise CheckError(f"Unknown excluded record(s): {', '.join(sorted(unknown))}")
    return excluded


def impact_for(bundle: EvidenceBundle, excluded_record_ids: list[str]) -> Impact:
    """Return potentially affected findings plus their remaining linked records."""
    excluded = _excluded(bundle, excluded_record_ids)
    fragments = {fragment.id: fragment.record_id for fragment in bundle.fragments}
    direct_records: dict[str, set[str]] = defaultdict(set)
    parents: dict[str, set[str]] = defaultdict(set)
    children: dict[str, set[str]] = defaultdict(set)
    for link in bundle.links:
        if link.relation not in SUPPORT_RELATIONS or link.target.type != "finding":
            continue
        target = link.target.id
        if link.source.type == "record":
            direct_records[target].add(link.source.id)
        elif link.source.type == "fragment":
            direct_records[target].add(fragments[link.source.id])
        elif link.source.type == "finding":
            parents[target].add(link.source.id)
            children[link.source.id].add(target)
        # An inference link can carry additional supporting source passages.
        direct_records[target].update(fragments[fid] for fid in link.fragment_ids)

    affected = {fid for fid, records in direct_records.items() if records & excluded}
    queue = deque(sorted(affected))
    while queue:
        for child in children[queue.popleft()]:
            if child not in affected:
                affected.add(child)
                queue.append(child)

    def remaining_records(finding_id: str) -> list[str]:
        found: set[str] = set()
        visited: set[str] = set()
        pending = [finding_id]
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            found.update(direct_records[current] - excluded)
            pending.extend(parents[current])
        return sorted(found)

    return Impact(
        affected_finding_ids=tuple(sorted(affected)),
        remaining_evidence={
            finding.snapshot_id: tuple(remaining_records(finding.snapshot_id))
            for finding in bundle.findings
        },
        excluded_record_ids=tuple(sorted(excluded)),
    )


def compute_impact(bundle: EvidenceBundle, excluded_record_ids: list[str]) -> dict:
    """JSON compatibility adapter; application code uses :func:`impact_for`."""
    return impact_for(bundle, excluded_record_ids).model_dump(mode="json")


def _normal_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def source_groups_for(bundle: EvidenceBundle) -> tuple[SourceGroup, ...]:
    """Group saved text copies separately from explicit original-to-repost links."""
    text_groups: dict[str, list[str]] = defaultdict(list)
    for record in bundle.records:
        normalized = _normal_text(record.text or "")
        if normalized:
            text_groups[normalized].append(record.id)
    groups = []
    for text, record_ids in sorted(text_groups.items()):
        if len(record_ids) < 2:
            continue
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        groups.append(
            {
                "id": f"same-text:{digest}",
                "kind": "exact_text",
                "label": "Same normalized text",
                "record_ids": sorted(record_ids),
                "origin_record_id": None,
                "independence": "unknown",
                "basis": "Identical saved text after Unicode and whitespace normalization; "
                "this does not establish who copied whom or a common origin.",
            }
        )

    descendants: dict[str, set[str]] = defaultdict(set)
    incoming: dict[str, set[str]] = defaultdict(set)
    basis: dict[tuple[str, str], str] = {}
    for link in bundle.links:
        if link.relation == "documented_repost":
            descendants[link.source.id].add(link.target.id)
            incoming[link.target.id].add(link.source.id)
            basis[link.source.id, link.target.id] = link.basis
    unseen = set(descendants) | set(incoming)
    while unseen:
        members: set[str] = set()
        pending = [min(unseen)]
        while pending:
            current = pending.pop()
            if current in members:
                continue
            members.add(current)
            pending.extend(descendants[current] | incoming[current])
        unseen -= members
        roots = sorted(rid for rid in members if not incoming[rid])
        degrees = {rid: len(incoming[rid]) for rid in members}
        ready = deque(roots)
        traversed = 0
        while ready:
            current = ready.popleft()
            traversed += 1
            for child in descendants[current]:
                degrees[child] -= 1
                if degrees[child] == 0:
                    ready.append(child)
        origin = roots[0] if len(roots) == 1 and traversed == len(members) else None
        reasons = {reason for (source, _), reason in basis.items() if source in members}
        digest = hashlib.sha256("\n".join(sorted(members)).encode()).hexdigest()[:16]
        groups.append(
            {
                "id": f"documented-repost:{origin or digest}",
                "kind": "documented_repost",
                "label": "Documented repost lineage" if origin else "Repost origin unresolved",
                "record_ids": sorted(members),
                "origin_record_id": origin,
                "independence": "unknown",
                "basis": "; ".join(sorted(reasons))
                + ("" if origin else "; conflicting or cyclic links do not establish one origin."),
            }
        )
    return tuple(SourceGroup.model_validate(group) for group in groups)


def group_sources(bundle: EvidenceBundle) -> list[dict]:
    """JSON compatibility adapter for integrations exporting source groups."""
    return [group.model_dump(mode="json") for group in source_groups_for(bundle)]


def validate_check(bundle: EvidenceBundle, result: dict) -> dict:
    """Validate structure, exact saved quotations, and declared search coverage."""
    try:
        checked = CheckOutput.model_validate({"findings": result.get("findings")})
    except (AttributeError, ValidationError) as exc:
        raise CheckError(f"Invalid verifier output: {exc}") from exc
    fragments = {fragment.id: fragment for fragment in bundle.fragments}
    records = {record.id: record for record in bundle.records}
    scope = result.get("search_scope", {})
    allowed = set(scope["fragment_ids"]) if "fragment_ids" in scope else None
    excluded = set(scope.get("excluded_record_ids", []))
    for item in checked.findings:
        if len(item.fragment_ids) != len(item.quotes):
            raise CheckError("Each cited fragment must have exactly one corresponding quote.")
        if len(set(item.fragment_ids)) != len(item.fragment_ids):
            raise CheckError("A finding cites the same fragment more than once.")
        for fid, quote in zip(item.fragment_ids, item.quotes, strict=True):
            if fid not in fragments:
                raise CheckError(f"Verifier cited an unknown fragment: {fid}")
            if allowed is not None and fid not in allowed:
                raise CheckError(f"Verifier cited a fragment outside its search scope: {fid}")
            fragment = fragments[fid]
            if fragment.record_id in excluded:
                raise CheckError(f"Verifier cited an excluded record: {fragment.record_id}")
            saved = fragment.quote
            record_text = records[fragment.record_id].text
            if not quote.strip() or saved is None or quote not in saved:
                raise CheckError(f"Verifier quotation does not match saved fragment: {fid}")
            if record_text is None or quote not in record_text:
                raise CheckError(f"Verifier quotation cannot be checked against record: {fid}")
    return {**result, "findings": [item.model_dump() for item in checked.findings]}


def _bounded_text(value: str, byte_limit: int) -> str:
    return value.encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore")


def _select_passages(bundle: EvidenceBundle, finding_id: str, excluded: set[str]) -> list[Passage]:
    finding = next(item for item in bundle.findings if item.snapshot_id == finding_id)
    record_texts = {record.id: record.text for record in bundle.records}
    words = set(re.findall(r"\w{3,}", finding.statement.casefold()))
    linked_fragments: set[str] = set()
    linked_records: set[str] = set()
    for link in bundle.links:
        if link.target.type == "finding" and link.target.id == finding_id:
            linked_fragments.update(link.fragment_ids)
            if link.source.type == "fragment":
                linked_fragments.add(link.source.id)
            elif link.source.type == "record":
                linked_records.add(link.source.id)
    candidates = [
        f
        for f in bundle.fragments
        if f.quote
        and f.record_id not in excluded
        and record_texts[f.record_id] is not None
        and f.quote in record_texts[f.record_id]
    ]

    def rank(fragment):
        score = len(words & set(re.findall(r"\w{3,}", fragment.quote.casefold())))
        directly_linked = fragment.id in linked_fragments or fragment.record_id in linked_records
        return (-int(directly_linked), -score, fragment.id)

    selected: list[Passage] = []
    record_ids: set[str] = set()
    consumed = 0
    for fragment in sorted(candidates, key=rank):
        if len(selected) >= MAX_FRAGMENTS:
            break
        if fragment.record_id not in record_ids and len(record_ids) >= MAX_RECORDS:
            continue
        remaining = MAX_PASSAGE_BYTES - consumed
        if remaining <= 0:
            break
        text = _bounded_text(fragment.quote, min(remaining, MAX_FRAGMENT_BYTES))
        if not text.strip():
            continue
        selected.append(
            {
                "fragment_id": fragment.id,
                "record_id": fragment.record_id,
                "text": text,
                "truncated": text != fragment.quote,
            }
        )
        record_ids.add(fragment.record_id)
        consumed += len(text.encode("utf-8"))
    return selected


def challenge_finding(
    bundle: EvidenceBundle,
    finding_id: str,
    excluded_ids: list[str] | None = None,
    mode: str = "demo",
    *,
    verifier: Verifier | None = None,
    bundle_fingerprint: str | None = None,
) -> dict:
    """Review one version-pinned finding with explicit demo or OpenRouter mode."""
    excluded = _excluded(bundle, excluded_ids)
    finding = next((item for item in bundle.findings if item.snapshot_id == finding_id), None)
    if finding is None:
        raise CheckError(f"Unknown finding: {finding_id}")
    if mode not in {"demo", "ollama", "openrouter"}:
        raise CheckError(f"Unsupported review mode: {mode}")
    if len(finding.statement.encode("utf-8")) > MAX_STATEMENT_BYTES:
        raise CheckError("Finding exceeds the review input limit; split it into atomic claims.")
    fragments = {fragment.id: fragment for fragment in bundle.fragments}
    queries = [finding.statement]
    scope = {
        "bundle_fingerprint": bundle_fingerprint or bundle.fingerprint(),
        "case_id": bundle.case.id,
        "finding_snapshot_id": finding_id,
        "run_id": finding.run_id,
        "total_record_count": len(bundle.records),
        "excluded_record_ids": sorted(excluded),
        "limitations": [
            "Only saved passages in this imported bundle are eligible. "
            "No issue found does not establish that the claim is true."
        ],
    }
    if mode == "demo":
        if not bundle.case.synthetic or "demo_check" not in finding.details:
            raise CheckError(
                "Demo checks are available only for an explicitly synthetic "
                "finding with a preset demo_check fixture."
            )
        preset = finding.details["demo_check"]
        raw_findings = preset.get("findings") if isinstance(preset, dict) else preset
        # Validate the fixture first; exclusions must not hide malformed fixtures.
        validated = validate_check(bundle, {"findings": raw_findings})
        selected = []
        for item in validated["findings"]:
            if not any(fragments[fid].record_id in excluded for fid in item["fragment_ids"]):
                selected.append(item)
        scope.update(
            {
                "retrieval_method": "synthetic fixture preset; no model or live search",
                "fragment_ids": sorted({fid for item in selected for fid in item["fragment_ids"]}),
                "omitted_fixture_findings": len(validated["findings"]) - len(selected),
            }
        )
        label = "Synthetic demo — preset review, not a live model result"
        result = {"findings": selected}
    else:
        passages = _select_passages(bundle, finding_id, excluded)
        scope.update(
            {
                "retrieval_method": "bounded lexical overlap plus directly linked passages",
                "fragment_ids": [p["fragment_id"] for p in passages],
                "record_ids": sorted({p["record_id"] for p in passages}),
                "truncated_fragment_ids": [p["fragment_id"] for p in passages if p["truncated"]],
                "input_bytes": sum(len(p["text"].encode("utf-8")) for p in passages),
            }
        )
        if not passages:
            raise CheckError("No eligible saved text passages are available for this review.")
        owned_verifier = verifier is None
        if verifier is None:
            verifier = (
                OllamaVerifier.from_env() if mode == "ollama" else OpenRouterVerifier.from_env()
            )
        try:
            result = verifier.check(finding.statement, passages)
        finally:
            if owned_verifier and hasattr(verifier, "close"):
                verifier.close()
        result = validate_check(bundle, {**result, "search_scope": scope})
        # Validate against the exact supplied text, not an unseen tail of a truncated passage.
        supplied = {p["fragment_id"]: p["text"] for p in passages}
        for item in result["findings"]:
            for fid, quote in zip(item["fragment_ids"], item["quotes"], strict=True):
                if fid not in supplied or not quote.strip() or quote not in supplied[fid]:
                    raise CheckError(f"Verifier quote was not in its supplied input: {fid}")
        provider = getattr(verifier, "provider", mode)
        label = (
            "Local model review — candidate objections for human assessment"
            if provider == "ollama"
            else "OpenRouter review — candidate objections for human assessment"
        )
        scope["model"] = verifier.model
        scope.update(
            provider=provider,
            requested_model=getattr(verifier, "requested_model", verifier.model),
            actual_model=getattr(verifier, "actual_model", None),
            prompt_version=PROMPT_VERSION,
            retrieval_version=RETRIEVAL_VERSION,
        )
    scope["record_count"] = len({fragments[fid].record_id for fid in scope["fragment_ids"]})
    return validate_check(
        bundle,
        {
            **result,
            "mode": mode,
            "label": label,
            "search_scope": scope,
            "queries": queries,
        },
    )
