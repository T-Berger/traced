"""One-way export through the existing Traced EvidenceBundle and package validator.

Specialist review states are saved provenance. They never manufacture a shared
Use, Qualify, or Withhold decision, and no shared application is initialized here.
This is the specialist package's dependency on Explorer's evidence contract and
package writer; storage, review history, and media interpretation remain local.
Bundle projection reads a saved snapshot, while package export also validates
current specialist state and stages hash-checked media in a temporary directory.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from urllib.parse import quote

from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.integrations.packages import (
    MAX_FILE_BYTES,
    export_case_package,
)

from .contracts import ReviewConflictError
from .media import resolve_frame_path
from .storage import canonical_json

MAX_SELECTED_MEDIA_BYTES = 60 * 1024 * 1024


def _id(*parts: object) -> str:
    return "silent-storm:" + ":".join(quote(str(part), safe="") for part in parts)


def _artifact(citation: dict) -> str:
    suffix = Path(citation["relative_path"]).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise ValueError("The cited artifact is not a supported supplied still image.")
    return f"media/{citation['media_sha256']}{suffix}"


def build_evidence_bundle(snapshot: dict) -> EvidenceBundle:
    """Project one frozen specialist brief, retaining original identifiers separately."""
    brief_id = snapshot["brief_id"]
    run_id = _id(brief_id, "run")
    origin = snapshot.get("source_origin", "unspecified")
    source_synthetic = {"real": False, "synthetic": True}.get(origin)
    # V1 bundles can already be imported under these immutable record and link IDs.
    link_frame_metadata = snapshot["schema_version"] >= 2
    records, fragments, findings, links = [], [], [], []
    record_ids: set[str] = set()

    def add_text(record_id: str, kind: str, label: str, value: dict) -> str:
        if record_id in record_ids:
            return record_id + ":fragment"
        text = canonical_json(value)
        records.append(
            {
                "id": record_id,
                "kind": kind,
                "source_label": label,
                "text": text,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                "metadata": {
                    "producer": "silent-storm",
                    "source_origin": origin,
                    "synthetic": source_synthetic,
                },
            }
        )
        fragment_id = record_id + ":fragment"
        fragments.append(
            {
                "id": fragment_id,
                "record_id": record_id,
                "selector": {"kind": "text", "start": 0, "end": len(text)},
                "quote": text,
            }
        )
        record_ids.add(record_id)
        return fragment_id

    def link(
        source_type: str,
        source_id: str,
        target_type: str,
        target_id: str,
        relation: str,
        basis: str,
        fragment_ids: list[str] | None = None,
    ):
        links.append(
            {
                "id": _id(brief_id, "link", len(links) + 1),
                "source": {"type": source_type, "id": source_id},
                "target": {"type": target_type, "id": target_id},
                "relation": relation,
                "basis": basis,
                "run_id": run_id,
                "fragment_ids": fragment_ids or [],
            }
        )

    history_record_id = _id(brief_id, "snapshot")
    history_fragment_id = add_text(
        history_record_id,
        "specialist_brief_snapshot",
        "Immutable specialist snapshot",
        snapshot,
    )
    evidence_records, evidence_fragments = {}, {}
    release = snapshot["release"]
    for citation in snapshot["citations"]:
        release_id, frame_id = citation["release_id"], citation["frame_id"]
        record_id = _id(release_id, "frame", frame_id)
        fragment_id = record_id + ":image"
        records.append(
            {
                "id": record_id,
                "kind": "image",
                "source_label": "Supplied historical frame",
                "content_hash": citation["media_sha256"],
                "artifact_ref": _artifact(citation),
                "metadata": {
                    **citation,
                    "producer": "silent-storm",
                    "source_origin": origin,
                    "synthetic": source_synthetic,
                    "specialist_evidence_id": citation["evidence_id"],
                    "first_posted_exercise_week": citation.get("week"),
                    "raw_week_label": citation.get("raw_metadata", {}).get("week"),
                    "posting_channel_affiliation": citation.get("side"),
                    "capture_time": None,
                    "coordinates": None,
                    "original_url": None,
                    "capture_order": None,
                },
            }
        )
        record_ids.add(record_id)
        fragments.append({"id": fragment_id, "record_id": record_id, "selector": {"kind": "image"}})
        metadata_id = record_id + ":metadata"
        metadata_fragment = add_text(
            metadata_id,
            "supplied_frame_metadata" if link_frame_metadata else "supplied_metadata",
            "Supplied CSV row and exact local provenance",
            citation,
        )
        if link_frame_metadata:
            link(
                "record",
                metadata_id,
                "record",
                record_id,
                "part_of",
                "This exact supplied CSV row describes the cited frame.",
                [metadata_fragment],
            )
        evidence_records[citation["evidence_id"]] = record_id
        evidence_fragments[citation["evidence_id"]] = [fragment_id, metadata_fragment]
        clip_key = citation.get("clip_key")
        if clip_key:
            clip_record = _id(release_id, "clip", clip_key)
            add_text(
                clip_record,
                "clip",
                "Supplied clip membership",
                {
                    "release_id": release_id,
                    "clip_key": clip_key,
                    "clip_id": citation.get("clip_id"),
                    "video_id": citation.get("video_id"),
                    "capture_order": None,
                },
            )
            link("record", record_id, "record", clip_record, "part_of", "Supplied clip membership.")
            if citation.get("video_id"):
                video_record = _id(release_id, "video", citation["video_id"])
                add_text(
                    video_record,
                    "source_video",
                    "Supplied source-video membership",
                    {
                        "release_id": release_id,
                        "video_id": citation["video_id"],
                        "original_publication_url": None,
                    },
                )
                link(
                    "record",
                    clip_record,
                    "record",
                    video_record,
                    "part_of",
                    "Supplied video membership; independence is not established.",
                )

    for review in snapshot["included"]:
        review_record = _id(brief_id, "review", review["review_id"], review["revision"])
        review_fragment = add_text(
            review_record,
            "specialist_review_revision",
            "Saved specialist revision; no shared disposition",
            review,
        )
        finding_id = _id(brief_id, "finding", review["review_id"], review["revision"])
        findings.append(
            {
                "snapshot_id": finding_id,
                "lineage_id": _id(review["scenario_id"], review["review_id"]),
                "version": review["revision"],
                "run_id": run_id,
                "kind": "observation",
                "statement": review["observation"],
                "details": {
                    "specialist_origin": {
                        key: review.get(key)
                        for key in (
                            "review_id",
                            "revision",
                            "scenario_id",
                            "release_id",
                            "origin_review_id",
                            "origin_revision",
                        )
                    },
                    "specialist_review_status": review["status"],
                    "specialist_review_record_ids": [review_record],
                    "specialist_history_context": {
                        "label": "Saved specialist history and context · read only",
                        "record_ref": {"type": "record", "id": history_record_id},
                        "fragment_ref": {"type": "fragment", "id": history_fragment_id},
                        "purpose": "Inspect frozen specialist review revisions, scenario lineage "
                        "and source-exclusion events.",
                        "limitation": "Read-only provenance, not additional source evidence, "
                        "current support, authenticity or a shared Use, Qualify or Withhold decision.",
                        "read_only": True,
                    },
                    "interpretation": review.get("interpretation", ""),
                    "uncertainty": review.get("uncertainty", ""),
                    "human_shared_disposition": None,
                },
            }
        )
        link(
            "fragment",
            review_fragment,
            "finding",
            finding_id,
            "depends_on",
            "The exact saved specialist revision supplies this observation.",
            [review_fragment],
        )
        for evidence_id in review["evidence_ids"]:
            if evidence_id not in evidence_records:
                raise ValueError("A briefing claim has an unresolved exact evidence reference.")
            for fragment_id in evidence_fragments[evidence_id]:
                link(
                    "fragment",
                    fragment_id,
                    "finding",
                    finding_id,
                    "supports",
                    "Evidence attached by the specialist reviewer; authenticity is not certified.",
                    [fragment_id],
                )

    scenario = snapshot["scenario"]
    limitations = [
        "Selected specialist evidence only; the complete archive and prior history are not packaged.",
        "Exercise week means first posted; capture times, coordinates, frame order and original URLs remain unknown.",
        "Posting-channel affiliation does not identify who is depicted.",
        "Specialist Reviewed records a workflow state, not authenticity or a shared Use decision.",
        "Reviewer and author names are manually entered, not authenticated identities.",
        "No automatic two-way synchronization or shared reviewer approval is supplied.",
    ]
    if origin == "unspecified":
        limitations.append(
            "Source origin was not declared; real or synthetic provenance is not inferred."
        )
    return EvidenceBundle.model_validate(
        {
            "schema_version": "1.0",
            "case": {
                "id": _id(brief_id, "case"),
                "title": snapshot["title"],
                "question": "Which saved specialist observations are supported by the selected local evidence?",
                "synthetic": source_synthetic is True,
                "coverage": {
                    "producer": "silent-storm",
                    "source_origin": origin,
                    "specialist_brief_id": brief_id,
                    "specialist_snapshot_fingerprint": snapshot["fingerprint"],
                    "selected_frame_count": len(snapshot["citations"]),
                    "included_statement_count": len(snapshot["included"]),
                    "omitted_statement_count": len(snapshot["omitted"]),
                    "release_id": release["release_id"],
                    "release_metadata_fingerprint": release["metadata_fingerprint"],
                },
                "limitations": limitations,
            },
            "records": records,
            "fragments": fragments,
            "findings": findings,
            "links": links,
            "runs": [
                {
                    "id": run_id,
                    "input_fingerprint": snapshot["fingerprint"],
                    "input_manifest": [row["id"] for row in records],
                    "method": "saved_specialist_review_snapshot",
                    "method_version": "1",
                    "created_at": snapshot["created_at"],
                    "full_inputs_available": False,
                    "recomputation_capability": "dependency_review_only",
                    "configuration": {
                        "scenario_id": scenario["scenario_id"],
                        "scenario_version": scenario["version"],
                        "review_versions": snapshot["review_versions"],
                    },
                    "coverage": {
                        "selected_frames": len(snapshot["citations"]),
                        "bounded_subset": True,
                    },
                }
            ],
            "scenarios": [
                {
                    "id": _id(brief_id, scenario["scenario_id"]),
                    "base_run_id": run_id,
                    "excluded_record_ids": [],
                    "status": "specialist_snapshot",
                    "reason": "Included frames were nonexcluded at snapshot finalization.",
                    "specialist_origin": scenario,
                    "specialist_lineage": snapshot.get("lineage"),
                    "specialist_exclusion_events": snapshot.get("exclusion_events"),
                }
            ],
            "reviews": [],
        }
    )


class SharedBriefExporter:
    """Adapt saved specialist briefs without creating shared cases or approvals.

    ``bundle`` projects frozen history without checking current media readiness.
    ``package`` requires a currently valid brief and copies only bounded selected
    media through the shared package writer. Neither method imports the result
    into an Evidence Trail workspace or changes specialist review state.
    """

    def __init__(self, store, briefs):
        self.store = store
        self.briefs = briefs

    def bundle(self, brief_id: str) -> EvidenceBundle:
        return build_evidence_bundle(self.briefs.get_brief(brief_id))

    def package(self, brief_id: str) -> bytes:
        """Materialize only this explicit selection, with native path/hash/ZIP checks."""
        validation = self.briefs.validate_brief(brief_id)
        if not validation["valid"]:
            raise ReviewConflictError(
                "This saved snapshot is history only. Preview and finalize current notes before packaging."
            )
        snapshot = self.briefs.get_brief(brief_id)
        bundle = build_evidence_bundle(snapshot)
        total = 0
        with tempfile.TemporaryDirectory(prefix="traced-selected-brief-") as temporary:
            artifact_root = Path(temporary)
            for citation in snapshot["citations"]:
                release = self.store.get_release(citation["release_id"])
                if (
                    release is None
                    or release["metadata_fingerprint"]
                    != snapshot["release"]["metadata_fingerprint"]
                ):
                    raise ReviewConflictError("The cited source release is unavailable or changed.")
                try:
                    source = resolve_frame_path(release["dataset_root"], citation["relative_path"])
                    with source.open("rb") as stream:
                        content = stream.read(MAX_FILE_BYTES + 1)
                except (OSError, ValueError) as exc:
                    raise ReviewConflictError(
                        "The cited image is unavailable; no package was produced."
                    ) from exc
                total += len(content)
                if len(content) > MAX_FILE_BYTES or total > MAX_SELECTED_MEDIA_BYTES:
                    raise ValueError("Selected images exceed the bounded package size.")
                if hashlib.sha256(content).hexdigest() != citation["media_sha256"]:
                    raise ReviewConflictError(
                        "A cited image changed; the package was not produced."
                    )
                destination = artifact_root / _artifact(citation)
                destination.parent.mkdir(exist_ok=True)
                destination.write_bytes(content)
            # Recheck after copying to catch an intervening note or exclusion save.
            if not self.briefs.validate_brief(brief_id)["valid"]:
                raise ReviewConflictError(
                    "Review state changed while packaging. Finalize a current snapshot."
                )
            return export_case_package(bundle, artifact_root)
