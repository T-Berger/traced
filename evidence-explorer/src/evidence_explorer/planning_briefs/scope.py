"""Pure saved-case scope identities, independent of live source subscriptions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..review.contracts import EvidenceBundle
from ..review.workflow import evidence_fingerprint
from .models import (
    BriefRevision,
    PlanningHistory,
    SavedRecordRef,
    ScopeIdentity,
    SourceObservation,
    canonical_json,
    digest,
)


def scope_identity(bundle: EvidenceBundle | Mapping[str, Any]) -> ScopeIdentity:
    """Describe supplied evidence without guessing collection or platform identities."""
    validated = EvidenceBundle.model_validate(bundle)
    data = validated.model_dump(mode="json")
    records = tuple(
        SavedRecordRef(
            record_id=row["id"],
            content_hash=row["content_hash"],
            source_label=row["source_label"],
            source_url=row.get("source_url"),
            metadata_json=canonical_json(
                {key: value for key, value in row.items() if key != "text"}
            ),
        )
        for row in sorted(data["records"], key=lambda row: row["id"])
    )
    raw_scope = data.get("collection_scope")
    coverage_scope = data["case"].get("coverage", {}).get("collection_scope")
    if raw_scope is not None and coverage_scope is not None:
        if canonical_json(raw_scope) != canonical_json(coverage_scope):
            raise ValueError("The saved collection scope declarations disagree.")
    raw_scope = raw_scope if raw_scope is not None else coverage_scope
    values: dict[str, Any] = {
        "case_id": validated.case.id,
        "evidence_fingerprint": evidence_fingerprint(data),
        "basis": "saved_evidence_manifest",
        "source_refs": tuple(record.record_id for record in records),
        "records": records,
    }
    if raw_scope is not None:
        if not isinstance(raw_scope, dict):
            raise ValueError("The saved collection scope must be an object.")
        refs = raw_scope.get("source_ids")
        sources = raw_scope.get("sources")
        manifest = raw_scope.get("observation_manifest")
        if (
            not isinstance(refs, list)
            or not refs
            or any(not isinstance(ref, str) or not ref.strip() for ref in refs)
            or len(refs) != len(set(refs))
            or not isinstance(sources, list)
            or any(not isinstance(source, dict) for source in sources)
            or sorted(source.get("id", "") for source in sources) != sorted(refs)
            or not isinstance(manifest, list)
        ):
            raise ValueError("The saved collection scope lacks exact source provenance.")
        observations = tuple(SourceObservation.model_validate(row) for row in manifest)
        observation_keys = [(row.source_id, row.item_key) for row in observations]
        if len(observation_keys) != len(set(observation_keys)) or any(
            row.source_id not in refs for row in observations
        ):
            raise ValueError("Saved observations must belong uniquely to the selected sources.")
        values.update(
            basis="collection_scope",
            source_refs=tuple(sorted(refs)),
            collection_scope_fingerprint=raw_scope.get("fingerprint"),
            observations_fingerprint=raw_scope.get("observations_fingerprint"),
            source_observations=observations,
            collection_scope_json=canonical_json(raw_scope),
        )
        if not values["collection_scope_fingerprint"] or not values["observations_fingerprint"]:
            raise ValueError("The saved collection scope has no durable observation fingerprint.")
    payload = ScopeIdentity.model_construct(**values, scope_fingerprint="0" * 64).model_dump(
        mode="json", exclude={"scope_fingerprint"}
    )
    values["scope_fingerprint"] = digest(payload)
    return ScopeIdentity.model_validate(values)


def same_saved_scope(left: ScopeIdentity, right: ScopeIdentity) -> bool:
    return left == right


def validate_history(
    history: PlanningHistory,
    *,
    existing_revisions: Mapping[str, BriefRevision] | None = None,
) -> None:
    """Validate references and hashes without granting imported records local authority."""
    history = PlanningHistory.model_validate(history)
    revisions = dict(existing_revisions or {})
    revisions.update((row.revision_id, row) for row in history.revisions)
    numbers = set()
    for revision in history.revisions:
        identity = (revision.brief_id, revision.number)
        if identity in numbers:
            raise ValueError("Planning history repeats a lineage revision number.")
        numbers.add(identity)
        if revision.parent_revision_id:
            parent = revisions.get(revision.parent_revision_id)
            if parent is None:
                raise ValueError("Planning history is missing a parent revision.")
            if parent.brief_id != revision.brief_id or parent.number >= revision.number:
                raise ValueError("Planning parent references must precede the same lineage.")
    for confirmation in history.confirmations:
        revision = revisions.get(confirmation.revision_id)
        if revision is None or revision.brief_id != confirmation.brief_id:
            raise ValueError("Planning confirmation references an unknown revision.")
        if revision.binding is None or confirmation.binding_digest != digest(
            revision.binding.model_dump(mode="json")
        ):
            raise ValueError("Planning confirmation binding digest mismatch.")


def validate_history_for_bundle(
    history: PlanningHistory, bundle: EvidenceBundle | Mapping[str, Any]
) -> None:
    """Validate complete relevant transport history, preserving older binding provenance."""
    validate_history(history)
    current = scope_identity(bundle)
    relevant = set()
    revisions = {revision.revision_id: revision for revision in history.revisions}
    for revision in history.revisions:
        binding = revision.binding
        if binding is None:
            continue
        if binding.case_id != current.case_id:
            raise ValueError("Planning history contains a binding for another case.")
        ancestor = revision
        while ancestor.revision_id not in relevant:
            relevant.add(ancestor.revision_id)
            if ancestor.parent_revision_id is None:
                break
            ancestor = revisions[ancestor.parent_revision_id]
        if binding.scope.evidence_fingerprint == current.evidence_fingerprint:
            if binding.scope != current:
                raise ValueError("Planning binding does not match the supplied saved source scope.")
    if any(revision.revision_id not in relevant for revision in history.revisions):
        raise ValueError("Planning history contains an unrelated unbound lineage.")
