"""Historical media adapter with explicit source context and bounded imports.

This adapter establishes supplied-file and metadata relationships. It does not
infer event dates, locations, depicted affiliations, or events from image counts.
The bundled demo uses harmless synthetic SVG cards, never conflict footage.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
from importlib.resources import files
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from ..contracts import EvidenceBundle

_VERSION = "1"
_DEMO_IMPORTED_AT = "2026-11-06T08:00:00+01:00"
_LIMITATIONS = [
    "Exercise week means the source video's first-posted exercise week, not capture time.",
    "Posting-channel affiliation does not identify the side depicted in a frame.",
    "Original publication URLs, capture dates, verified locations, and event identity are unknown.",
    "Frame, clip, and source-video counts describe supplied evidence, not real-world event counts.",
    "Identical file bytes establish repeated supplied content, not an independent origin.",
]


def media_artifact_root() -> Path:
    """Return the trusted root resolving demo ``artifact_ref`` values.

    The source package and normal installed wheel expose these package assets on
    disk. Importers should copy the referenced files into their own evidence store
    and retain the relative names when preparing a portable case.
    """
    return Path(str(files(__package__).joinpath("data")))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _text_record(record_id: str, kind: str, label: str, value: Any, imported_at: str) -> dict:
    text = _json(value)
    return {
        "id": record_id,
        "kind": kind,
        "source_label": label,
        "text": text,
        "content_hash": _hash_bytes(text.encode("utf-8")),
        "source_url": None,
        "published_at": None,
        "acquired_at": imported_at,
        "metadata": {"supplied_metadata": True},
    }


def _safe_artifact(root: Path, value: Any) -> tuple[str, Path]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Each frame needs a nonempty relative file reference.")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise ValueError("Frame file references must be safe relative paths.")
    candidate = root.joinpath(*relative.parts)
    if candidate.is_symlink():
        raise ValueError("Frame file references cannot be symbolic links.")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("Frame file reference leaves the trusted artifact root.")
    if not resolved.is_file():
        raise ValueError(f"Supplied frame file does not exist: {value}")
    return relative.as_posix(), resolved


def build_reviewed_media_bundle(
    rows: Iterable[Mapping[str, Any]],
    *,
    artifact_root: Path,
    release_id: str,
    imported_at: str,
    synthetic: bool = False,
    max_frames: int = 100,
) -> EvidenceBundle:
    """Adapt an explicitly selected metadata subset and trusted local files.

    ``rows`` uses the supplied Silent Storm column names: ``frame_id``, ``file``,
    ``clip_id``, ``video_id``, ``week`` and ``side``. The caller chooses the subset;
    exceeding the bound fails rather than silently presenting partial coverage.
    ``imported_at`` is the preserved acquisition timestamp for this release and
    must be reused when recreating the same immutable import. The adapter creates
    metadata observations only; it does not promote automated detections or raw
    image interpretation into reviewed facts.
    """
    if not isinstance(release_id, str) or not release_id.strip():
        raise ValueError("A release identifier is required.")
    if isinstance(max_frames, bool) or not isinstance(max_frames, int) or max_frames < 1:
        raise ValueError("max_frames must be a positive integer.")
    timestamp = datetime.fromisoformat(imported_at.replace("Z", "+00:00"))
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("The release acquisition timestamp must include a timezone.")
    root = Path(artifact_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("The trusted artifact root must be an existing directory.")
    selected = list(islice(rows, max_frames + 1))
    if not selected:
        raise ValueError("Select at least one supplied frame.")
    if len(selected) > max_frames:
        raise ValueError(f"Select at most {max_frames} frames for this bounded import.")
    selected = sorted((dict(row) for row in selected), key=lambda row: str(row.get("frame_id")))
    prefix = "media:" + quote(release_id.strip(), safe="")
    records: list[dict] = []
    fragments: list[dict] = []
    links: list[dict] = []
    hashes: dict[str, list[str]] = defaultdict(list)
    frame_ids: set[str] = set()
    clip_videos: dict[str, str] = {}
    video_ids: set[str] = set()
    metadata_fragments: list[str] = []
    frame_record_ids: list[str] = []

    def identifier(kind: str, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Each frame needs a nonempty {kind} identifier.")
        return f"{prefix}:{kind}:" + quote(value, safe="")

    def link(
        source_type: str,
        source: str,
        target_type: str,
        target: str,
        relation: str,
        basis: str,
        fragment_ids: list[str] | None = None,
    ) -> None:
        links.append(
            {
                "id": f"{prefix}:link:{len(links) + 1}",
                "source": {"type": source_type, "id": source},
                "target": {"type": target_type, "id": target},
                "relation": relation,
                "basis": basis,
                "fragment_ids": fragment_ids or [],
            }
        )

    for row in selected:
        frame_id = identifier("frame", row.get("frame_id"))
        clip_id = identifier("clip", row.get("clip_id"))
        video_id = identifier("video", row.get("video_id"))
        if frame_id in frame_ids:
            raise ValueError(f"Duplicate frame identifier: {row['frame_id']}")
        frame_ids.add(frame_id)
        if clip_id in clip_videos and clip_videos[clip_id] != video_id:
            raise ValueError("A supplied clip identifier cannot refer to different source videos.")
        clip_videos[clip_id] = video_id
        video_ids.add(video_id)
        week = row.get("week")
        if isinstance(week, bool) or not str(week).isdigit() or int(week) < 1:
            raise ValueError("week must be a positive first-posted exercise-week number.")
        affiliation = row.get("side")
        if affiliation is not None and not isinstance(affiliation, str):
            raise ValueError("Posting-channel affiliation must be text or unknown.")
        artifact_ref, path = _safe_artifact(root, row.get("file"))
        digest = _hash_bytes(path.read_bytes())
        metadata = {
            "release_id": release_id,
            "frame_id": row["frame_id"],
            "clip_id": row["clip_id"],
            "video_id": row["video_id"],
            "first_posted_exercise_week": int(week),
            "posting_channel_affiliation": affiliation or None,
            "depicted_side": None,
            "capture_time": None,
            "event_time": None,
            "location": None,
            "original_publication_context": "unavailable",
            "synthetic": synthetic,
        }
        records.append(
            {
                "id": frame_id,
                "kind": "supplied_frame",
                "source_label": f"Supplied local frame {row['frame_id']}",
                "artifact_ref": artifact_ref,
                "content_hash": digest,
                "source_url": None,
                "published_at": None,
                "acquired_at": imported_at,
                "metadata": metadata,
            }
        )
        frame_record_ids.append(frame_id)
        hashes[digest].append(frame_id)
        image_fragment_id = frame_id + ":image"
        fragments.append(
            {
                "id": image_fragment_id,
                "record_id": frame_id,
                "selector": {"kind": "image"},
            }
        )
        metadata_id = frame_id + ":metadata"
        metadata_value = {"raw_supplied_row": row, "interpretation": metadata}
        metadata_record = _text_record(
            metadata_id,
            "supplied_frame_metadata",
            f"Metadata for {row['frame_id']}",
            metadata_value,
            imported_at,
        )
        records.append(metadata_record)
        meta_fragment_id = metadata_id + ":fragment"
        metadata_fragments.append(meta_fragment_id)
        fragments.append(
            {
                "id": meta_fragment_id,
                "record_id": metadata_id,
                "selector": {"kind": "text", "start": 0, "end": len(metadata_record["text"])},
                "quote": metadata_record["text"],
            }
        )
        link(
            "record",
            frame_id,
            "record",
            clip_id,
            "part_of",
            "Supplied clip_id groups frames from the same continuous shot.",
            [meta_fragment_id],
        )
        link(
            "record",
            metadata_id,
            "record",
            frame_id,
            "part_of",
            "Supplied metadata row describes this frame.",
            [meta_fragment_id],
        )

    for clip_id, video_id in sorted(clip_videos.items()):
        records.append(
            _text_record(
                clip_id,
                "supplied_clip",
                "Supplied continuous-shot identifier",
                {
                    "clip_record_id": clip_id,
                    "source_video_record_id": video_id,
                    "capture_time": None,
                    "event_time": None,
                    "location": None,
                },
                imported_at,
            )
        )
        link(
            "record",
            clip_id,
            "record",
            video_id,
            "part_of",
            "Supplied video_id identifies the source video; it does not identify an event.",
        )
    for video_id in sorted(video_ids):
        records.append(
            _text_record(
                video_id,
                "supplied_source_video",
                "Supplied source-video identifier",
                {
                    "source_video_record_id": video_id,
                    "original_source_url": None,
                    "capture_time": None,
                    "event_time": None,
                    "location": None,
                },
                imported_at,
            )
        )

    repeated = [members for _, members in sorted(hashes.items()) if len(members) > 1]
    for members in repeated:
        for member in members[1:]:
            link(
                "record",
                members[0],
                "record",
                member,
                "same_content",
                "The saved image files have identical SHA-256 byte hashes; origin is unknown.",
            )

    fingerprint = _hash_bytes(
        _json(
            [{"id": record["id"], "content_hash": record["content_hash"]} for record in records]
        ).encode("utf-8")
    )
    run_id = f"{prefix}:run:{fingerprint[:20]}"
    overview_id = f"{prefix}:finding:coverage:{fingerprint[:20]}"
    findings = [
        {
            "snapshot_id": overview_id,
            "lineage_id": f"{prefix}:finding:coverage",
            "version": 1,
            "run_id": run_id,
            "kind": "observation",
            "statement": (
                f"The selected supplied subset contains {len(frame_ids)} frame records, "
                f"{len(clip_videos)} clip identifiers, and "
                f"{len(video_ids)} source-video identifiers."
            ),
            "details": {
                "domain": "historical_media",
                "counts_describe": "supplied_evidence",
                "first_posted_exercise_weeks": sorted({int(row["week"]) for row in selected}),
                "event_count": None,
                "location": None,
                "capture_time": None,
            },
        }
    ]
    for fragment_id in metadata_fragments:
        link(
            "fragment",
            fragment_id,
            "finding",
            overview_id,
            "supports",
            "The supplied metadata records frame, clip, and source-video identifiers.",
            [fragment_id],
        )
    if repeated:
        duplicate_id = f"{prefix}:finding:repeated-content:{fingerprint[:20]}"
        findings.append(
            {
                "snapshot_id": duplicate_id,
                "lineage_id": f"{prefix}:finding:repeated-content",
                "version": 1,
                "run_id": run_id,
                "kind": "observation",
                "statement": (
                    f"{sum(len(members) for members in repeated)} supplied frame records "
                    f"belong to {len(repeated)} repeated-content group(s) "
                    "with identical file bytes."
                ),
                "details": {
                    "domain": "historical_media",
                    "same_content_groups": repeated,
                    "independent_origins": None,
                    "event_count": None,
                },
            }
        )
        for members in repeated:
            for record_id in members:
                fragment_id = record_id + ":image"
                link(
                    "fragment",
                    fragment_id,
                    "finding",
                    duplicate_id,
                    "supports",
                    "Exact supplied-file hash comparison establishes repeated bytes only.",
                    [fragment_id],
                )

    coverage = {
        "selected_frames": len(frame_ids),
        "selected_clips": len(clip_videos),
        "selected_source_videos": len(video_ids),
        "bounded_subset": True,
        "full_release_processed": False,
    }
    return EvidenceBundle.model_validate(
        {
            "schema_version": "1.0",
            "case": {
                "id": prefix + ":case",
                "title": (
                    "Synthetic historical media review"
                    if synthetic
                    else "Historical supplied media review"
                ),
                "question": "What does this supplied evidence establish, and what remains unknown?",
                "synthetic": synthetic,
                "coverage": coverage,
                "limitations": _LIMITATIONS,
            },
            "records": records,
            "fragments": fragments,
            "runs": [
                {
                    "id": run_id,
                    "input_fingerprint": fingerprint,
                    "input_manifest": [record["id"] for record in records],
                    "method": "supplied-media-metadata-and-byte-hashes",
                    "method_version": _VERSION,
                    "created_at": imported_at,
                    "full_inputs_available": True,
                    "recomputation_capability": "dependency_review_only",
                    "configuration": {"release_id": release_id, "max_frames": max_frames},
                    "coverage": coverage,
                }
            ],
            "findings": findings,
            "links": links,
        }
    )


def build_media_demo_bundle() -> EvidenceBundle:
    """Return a deterministic synthetic case with a deliberately unsupported inference."""
    rows = [
        {
            "frame_id": "frame-a",
            "file": "frames/card-a.svg",
            "clip_id": "clip-a",
            "video_id": "video-a",
            "week": 11,
            "side": "Blue Force",
        },
        {
            "frame_id": "frame-a-copy",
            "file": "frames/card-a-copy.svg",
            "clip_id": "clip-b",
            "video_id": "video-b",
            "week": 12,
            "side": "Rusviet",
        },
        {
            "frame_id": "frame-b",
            "file": "frames/card-b.svg",
            "clip_id": "clip-b",
            "video_id": "video-b",
            "week": 12,
            "side": "Rusviet",
        },
    ]
    bundle = build_reviewed_media_bundle(
        rows,
        artifact_root=media_artifact_root(),
        release_id="synthetic-historical-v1",
        imported_at=_DEMO_IMPORTED_AT,
        synthetic=True,
    )
    payload = bundle.model_dump(mode="json")
    run_id = bundle.runs[0].id
    prefix = bundle.case.id.removesuffix(":case")
    inference_id = prefix + ":finding:single-event:v1"
    metadata_fragment = next(f for f in bundle.fragments if f.record_id.endswith(":metadata"))
    record = next(r for r in bundle.records if r.id == metadata_fragment.record_id)
    payload["findings"].append(
        {
            "snapshot_id": inference_id,
            "lineage_id": prefix + ":finding:single-event",
            "version": 1,
            "run_id": run_id,
            "kind": "inference",
            "statement": "These supplied frames document a single historical event.",
            "details": {
                "domain": "historical_media",
                "synthetic": True,
                "assumptions": ["Repeated material and exercise weeks identify one event."],
                "demo_check": [
                    {
                        "finding_type": "unsupported_inference",
                        "explanation": (
                            "The metadata provides first-posted exercise weeks and supplied-file "
                            "relationships. It does not establish capture time, location, "
                            "or event identity."
                        ),
                        "fragment_ids": [metadata_fragment.id],
                        "quotes": [record.text],
                    }
                ],
            },
        }
    )
    for index, finding in enumerate(bundle.findings, start=1):
        payload["links"].append(
            {
                "id": f"{prefix}:inference-premise:{index}",
                "source": {"type": "finding", "id": finding.snapshot_id},
                "target": {"type": "finding", "id": inference_id},
                "relation": "depends_on",
                "fragment_ids": [],
                "run_id": run_id,
                "basis": "Synthetic imported inference uses this observation as a premise.",
            }
        )
    payload["reviews"] = [
        {
            "id": prefix + ":review:single-event-withheld",
            "finding_snapshot_id": inference_id,
            "run_id": run_id,
            "reviewer": "Synthetic reviewer",
            "disposition": "withhold",
            "reason": "The supplied metadata does not establish that the frames depict one event.",
            "evidence_refs": [{"type": "fragment", "id": metadata_fragment.id}],
            "timestamp": _DEMO_IMPORTED_AT,
            "origin": "synthetic_review_fixture",
        }
    ]
    return EvidenceBundle.model_validate(payload)
