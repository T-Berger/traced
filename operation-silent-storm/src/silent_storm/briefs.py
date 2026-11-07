"""Bounded, immutable cited briefs from saved specialist review revisions.

Finalization checks scenario/revision versions and hashes selected local bytes
while holding a SQLite write transaction, then saves a specialist snapshot.
The service owns validation, current-state checks, and byte verification.
Connection-bound repositories capture history and persist snapshots; pure renderers
serialize frozen payloads. Review decisions, imported evidence, and the independent
media-checksum job remain unchanged; ``shared_brief`` owns the shared-package adapter.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from uuid import uuid4

from .brief_rendering import _markdown_text as _markdown_text
from .brief_rendering import json_snapshot, markdown_snapshot
from .brief_repository import BriefRepository
from .contracts import ReviewConflictError, ReviewNotFoundError, ReviewServiceProtocol
from .media import resolve_frame_path
from .review_repository import ReviewRepository
from .storage import Store, canonical_json, fingerprint, utc_now

MAX_BRIEF_REVIEWS = 12
MAX_BRIEF_FRAMES = 12
MAX_MEDIA_FILE_BYTES = 20 * 1024 * 1024
MAX_SELECTED_MEDIA_BYTES = 60 * 1024 * 1024
MAX_LINEAGE_ITEMS = 12
MAX_EXCLUSION_EVENTS = 12
MAX_REVIEW_HISTORY = 48
MAX_SNAPSHOT_JSON_BYTES = 2 * 1024 * 1024
SOURCE_ORIGINS = frozenset({"unspecified", "real", "synthetic"})
NOTE_FIELDS = (
    "review_id",
    "scenario_id",
    "release_id",
    "revision",
    "title",
    "observation",
    "interpretation",
    "uncertainty",
    "rationale",
    "reviewer",
    "status",
    "evidence_ids",
    "created_at",
    "change_type",
    "change_reason",
    "origin_review_id",
    "origin_revision",
)
RELEASE_FIELDS = (
    "release_id",
    "title",
    "metadata_fingerprint",
    "frames_csv_sha256",
    "detections_csv_sha256",
    "imported_at",
    "counts",
)
CITATION_FRAME_FIELDS = (
    "release_id",
    "frame_id",
    "clip_key",
    "clip_id",
    "video_id",
    "file",
    "relative_path",
    "source_file",
    "source_row",
    "raw_metadata",
    "week",
    "side",
)
CONTEXT = {
    "kind": "saved_specialist_review",
    "review_status_meaning": "Reviewed records a human workflow decision, not media authenticity.",
    "capture_time": "not supplied",
    "capture_order": "not supplied",
    "coordinates": "not supplied",
    "original_source_urls": "not supplied",
    "unit_identities": "not supplied",
    "posting_week": "Exercise week when the source video was first posted; not capture time.",
    "posting_side": "Posting-channel affiliation; not an identification of who is depicted.",
    "detectors": "Automated and unverified; no detector decision is promoted into a review.",
}


def _text(value: str, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{name} must be nonempty text of at most {limit} characters.")
    return value.strip()


def _review_ids(values: list[str]) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_BRIEF_REVIEWS:
        raise ValueError(f"Select between 1 and {MAX_BRIEF_REVIEWS} saved statements.")
    if any(not isinstance(value, str) or not value or len(value) > 500 for value in values):
        raise ValueError("Saved statement IDs must be nonempty strings.")
    if len(set(values)) != len(values):
        raise ValueError("Select each saved statement only once.")
    return list(values)


def _versions(values: dict[str, int]) -> dict[str, int]:
    if not isinstance(values, dict):
        raise ValueError("review_versions must map saved statement IDs to their revisions.")
    _review_ids(list(values))
    if any(type(value) is not int or value < 1 for value in values.values()):
        raise ValueError("Every expected review revision must be a positive integer.")
    return dict(values)


def _reasons(values) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value))


def _signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _fresh_digest(
    root: str | Path, relative_path: str, remaining_bytes: int, *, budget: list[int] | None = None
) -> dict[str, Any]:
    """Verify a constrained still image without reading an unbounded file into memory."""
    remaining_bytes = min(remaining_bytes, budget[0]) if budget is not None else remaining_bytes
    path = resolve_frame_path(root, relative_path)
    preliminary = path.stat()
    if preliminary.st_size > MAX_MEDIA_FILE_BYTES:
        raise ValueError("Media exceeds the 20 MiB per-file limit.")
    if preliminary.st_size > remaining_bytes:
        raise ValueError("Selected media exceeds the 60 MiB total limit.")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or _signature(before) != _signature(preliminary):
            raise ValueError("Media changed before byte verification.")
        digest, size = hashlib.sha256(), 0
        ceiling = min(MAX_MEDIA_FILE_BYTES, remaining_bytes)
        while block := stream.read(min(1024 * 1024, ceiling - size + 1)):
            size += len(block)
            if budget is not None:
                budget[0] -= len(block)
            if size > ceiling:
                raise ValueError("Media grew beyond the selected byte limit during verification.")
            digest.update(block)
        after = os.fstat(stream.fileno())
    current = resolve_frame_path(root, relative_path)
    if (
        current != path
        or _signature(before) != _signature(after)
        or (_signature(current.stat()) != _signature(after) or size != after.st_size)
    ):
        raise ValueError("Media changed during byte verification.")
    return {
        "media_sha256": digest.hexdigest(),
        "media_size_bytes": size,
        "path": path,
        "signature": _signature(after),
    }


def _safe_error(error: Exception) -> str:
    return (
        f"Media unavailable ({type(error).__name__})." if isinstance(error, OSError) else str(error)
    )


def _recheck_proof(root: str | Path, relative_path: str, proof: dict[str, Any]) -> None:
    path = resolve_frame_path(root, relative_path)
    if path != proof["path"] or _signature(path.stat()) != proof["signature"]:
        raise ValueError("Selected media changed after byte verification.")


class BriefService:
    """Persist and inspect frozen specialist briefs in the existing local store.

    Construction initializes brief tables. Preview/validation inspect current
    specialist state and local media; finalization saves an immutable snapshot
    with revision guards. Shared EvidenceBundle serialization is delegated to
    ``SharedBriefExporter`` rather than represented as a local review decision.
    """

    def __init__(self, store: Store, reviews: ReviewServiceProtocol):
        self.store, self.reviews = store, reviews
        with store.connection() as connection:
            BriefRepository(connection).initialize()

    def _selected(self, scenario_id: str, review_ids: list[str]) -> list[dict[str, Any]]:
        notes = []
        for review_id in _review_ids(review_ids):
            note = self.reviews.get_review(review_id)
            if note["scenario_id"] != scenario_id:
                raise ReviewNotFoundError("Saved statement is not in the selected scenario.")
            notes.append(note)
        evidence_ids = {evidence for note in notes for evidence in note["evidence_ids"]}
        if len(evidence_ids) > MAX_BRIEF_FRAMES:
            raise ValueError(
                f"Select at most {MAX_BRIEF_FRAMES} unique evidence frames; none were truncated."
            )
        return notes

    @staticmethod
    def _assess(notes: list[dict], release: dict, *, verify_bytes: bool):
        frames = {frame["evidence_id"]: frame for note in notes for frame in note["evidence"]}
        errors, verified, warnings = {}, {}, []
        available_bytes = 0
        # Size-check the entire bounded selection before hashing any file.
        for evidence_id, frame in frames.items():
            try:
                path = resolve_frame_path(release["dataset_root"], frame["relative_path"])
                size = path.stat().st_size
                if size > MAX_MEDIA_FILE_BYTES:
                    raise ValueError("Media exceeds the 20 MiB per-file limit.")
                available_bytes += size
                digest = frame.get("recorded_media_sha256") or frame.get("media_sha256")
                if (
                    frame.get("hash_status") != "hashed"
                    or not isinstance(digest, str)
                    or not (re.fullmatch(r"[0-9a-f]{64}", digest))
                ):
                    raise ValueError(
                        "A completed media SHA-256 is required; this file is uncomputed or failed."
                    )
                if frame.get("hash_stat_matches") is False:
                    warnings.append(
                        f"{evidence_id}: file metadata differs from the recorded checksum; "
                        "finalization must verify the current bytes."
                    )
            except (OSError, ValueError) as error:
                errors[evidence_id] = _safe_error(error)
        if available_bytes > MAX_SELECTED_MEDIA_BYTES:
            raise ValueError("Selected media exceeds the 60 MiB total limit; select fewer frames.")
        consumed, budget = 0, [MAX_SELECTED_MEDIA_BYTES]
        if verify_bytes:
            for evidence_id, frame in frames.items():
                if evidence_id in errors:
                    continue
                try:
                    proof = _fresh_digest(
                        release["dataset_root"],
                        frame["relative_path"],
                        MAX_SELECTED_MEDIA_BYTES - consumed,
                        budget=budget,
                    )
                    consumed += proof["media_size_bytes"]
                    recorded = frame.get("recorded_media_sha256") or frame.get("media_sha256")
                    if proof["media_sha256"] != recorded:
                        raise ValueError(
                            "Current media bytes differ from the completed recorded checksum."
                        )
                    verified[evidence_id] = proof
                except (OSError, ValueError) as error:
                    errors[evidence_id] = _safe_error(error)
        included, omitted, items = [], [], []
        for note in notes:
            reasons = list(note["eligibility"]["reasons"])
            if note["status"] != "reviewed":
                reasons.append("Only a saved current Reviewed revision can enter this brief.")
            if not note.get("is_current", True):
                reasons.append("This statement is not the current saved revision.")
            reasons.extend(f"{key}: {errors[key]}" for key in note["evidence_ids"] if key in errors)
            reasons = _reasons(reasons)
            item = {
                **note,
                "brief_eligibility": {
                    "eligible": not reasons,
                    "reasons": reasons,
                    "provisional": not verify_bytes,
                },
            }
            items.append(item)
            if reasons:
                omitted.append(
                    {key: note[key] for key in ("review_id", "revision", "title", "status")}
                    | {"reasons": reasons}
                )
            else:
                included.append(item)
        return items, included, omitted, frames, verified, _reasons(warnings)

    def preview(self, scenario_id: str, review_ids: list[str]) -> dict[str, Any]:
        scenario = self.reviews.get_scenario(scenario_id)
        notes = self._selected(scenario_id, review_ids)
        release = self.store.get_release(scenario["release_id"])
        if release is None:
            raise ReviewNotFoundError("Release not found.")
        items, included, omitted, _, _, warnings = self._assess(notes, release, verify_bytes=False)
        if self.reviews.get_scenario(scenario_id)["version"] != scenario["version"]:
            raise ReviewConflictError("The scenario changed while previewing. Refresh the preview.")
        return {
            "scenario": scenario,
            "items": items,
            "included": included,
            "omitted": omitted,
            "review_versions": {note["review_id"]: note["revision"] for note in notes},
            "expected_version": scenario["version"],
            "provisional": True,
            "warnings": [
                "Preview checks recorded hashes and current file metadata; finalization "
                "rehashes the selected bytes.",
                *warnings,
            ],
            "limits": {
                "reviews": MAX_BRIEF_REVIEWS,
                "frames": MAX_BRIEF_FRAMES,
                "media_file_bytes": MAX_MEDIA_FILE_BYTES,
                "selected_media_bytes": MAX_SELECTED_MEDIA_BYTES,
            },
        }

    @staticmethod
    def _origin(connection: sqlite3.Connection, note: dict):
        return BriefRepository(connection).origin(note)

    @staticmethod
    def _lineage(connection: sqlite3.Connection, scenario: dict):
        return BriefRepository(connection).lineage(scenario, MAX_LINEAGE_ITEMS)

    @staticmethod
    def _events(connection: sqlite3.Connection, scenario_id: str):
        return BriefRepository(connection).events(scenario_id, MAX_EXCLUSION_EVENTS)

    @staticmethod
    def _history(connection: sqlite3.Connection, review_ids: list[str]):
        return BriefRepository(connection).history(review_ids, MAX_REVIEW_HISTORY)

    def create(
        self,
        scenario_id: str,
        *,
        title: str,
        reviewer: str,
        review_versions: dict[str, int],
        expected_version: int,
        source_origin: str = "unspecified",
    ) -> dict[str, Any]:
        title, reviewer = _text(title, "Title", 300), _text(reviewer, "Reviewer", 150)
        versions = _versions(review_versions)
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("Expected scenario version must be a nonnegative integer.")
        if not isinstance(source_origin, str) or source_origin not in SOURCE_ORIGINS:
            raise ValueError("Evidence origin must be unspecified, real, or synthetic.")
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            scenario = self.reviews.get_scenario(scenario_id)
            if scenario["version"] != expected_version:
                raise ReviewConflictError(
                    "The scenario changed after preview. Preview the brief again."
                )
            notes = self._selected(scenario_id, list(versions))
            if any(note["revision"] != versions[note["review_id"]] for note in notes):
                raise ReviewConflictError(
                    "A selected statement changed after preview. Preview again."
                )
            release = self.store.get_release(scenario["release_id"])
            if release is None:
                raise ReviewNotFoundError("Release not found.")
            _, included, omitted, frames, verified, _ = self._assess(
                notes, release, verify_bytes=True
            )
            if not included:
                raise ValueError(
                    "No selected statement has current Reviewed evidence and valid completed hashes."
                )
            citations, seen = [], set()
            for note in included:
                for evidence_id in note["evidence_ids"]:
                    if evidence_id in seen:
                        continue
                    seen.add(evidence_id)
                    frame, proof = frames[evidence_id], verified[evidence_id]
                    href = "/evidence?" + urlencode(
                        {
                            "release_id": frame["release_id"],
                            "frame_id": frame["frame_id"],
                            "sha256": proof["media_sha256"],
                        },
                        quote_via=quote,
                    )
                    citations.append(
                        {
                            **{key: frame.get(key) for key in CITATION_FRAME_FIELDS},
                            "citation_id": f"citation-{len(citations) + 1}",
                            "evidence_id": evidence_id,
                            "href": href,
                            "media_sha256": proof["media_sha256"],
                            "media_size_bytes": proof["media_size_bytes"],
                        }
                    )
            payload = {
                "schema_version": 2,
                "brief_id": "brief-" + uuid4().hex,
                "snapshot_kind": "saved_specialist_review",
                "source_origin": source_origin,
                "title": title,
                "reviewer": reviewer,
                "created_at": utc_now(),
                "scenario": scenario,
                "release": {key: release.get(key) for key in RELEASE_FIELDS},
                "review_versions": versions,
                "included": [
                    {
                        **{key: note.get(key) for key in NOTE_FIELDS},
                        "origin": self._origin(connection, note),
                    }
                    for note in included
                ],
                "selected_reviews": [
                    {
                        **{key: note.get(key) for key in NOTE_FIELDS},
                        "origin": self._origin(connection, note),
                    }
                    for note in notes
                ],
                "omitted": omitted,
                "citations": citations,
                "review_history": self._history(connection, list(versions)),
                "lineage": self._lineage(connection, scenario),
                "exclusion_events": self._events(connection, scenario_id),
                "context": dict(CONTEXT),
            }
            digest = fingerprint(payload)
            if (
                len(
                    json.dumps(
                        {**payload, "fingerprint": digest}, ensure_ascii=False, indent=2
                    ).encode("utf-8")
                )
                > MAX_SNAPSHOT_JSON_BYTES
            ):
                raise ValueError(
                    "The selected statements and saved history exceed the 2 MiB JSON "
                    "reopen limit. Select fewer statements or use a fork with shorter "
                    "history; no content was silently truncated."
                )
            snapshot_json = canonical_json(payload)
            for citation in citations:
                try:
                    _recheck_proof(
                        release["dataset_root"],
                        citation["relative_path"],
                        verified[citation["evidence_id"]],
                    )
                except (OSError, ValueError) as error:
                    raise ReviewConflictError(
                        "Selected media changed during finalization. Preview again."
                    ) from error
            BriefRepository(connection).insert_snapshot(
                payload, snapshot_json, digest, scenario_id=scenario_id
            )
        return {**payload, "fingerprint": digest}

    def get_brief(self, brief_id: str) -> dict[str, Any]:
        with self.store.connection() as connection:
            row = BriefRepository(connection).snapshot(brief_id)
        if row is None:
            raise ReviewNotFoundError("Saved brief not found.")
        payload = json.loads(row["snapshot_json"])
        if fingerprint(payload) != row["fingerprint"]:
            raise ValueError("Saved brief content does not match its fingerprint.")
        return {**payload, "fingerprint": row["fingerprint"]}

    def list_briefs(self, scenario_id: str, page: int = 1, page_size: int = 12) -> dict[str, Any]:
        self.reviews.get_scenario(scenario_id)
        if (
            type(page) is not int
            or not 1 <= page <= 1_000_000
            or (type(page_size) is not int or not 1 <= page_size <= 12)
        ):
            raise ValueError(
                "Brief pages must be positive integers with page_size between 1 and 12."
            )
        with self.store.connection() as connection:
            total, items = BriefRepository(connection).page(
                scenario_id,
                page_size,
                (page - 1) * page_size,
            )
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def validate_brief(self, brief_id: str) -> dict[str, Any]:
        saved = self.get_brief(brief_id)
        reasons, note_results, citation_results = [], [], []
        try:
            scenario = self.reviews.get_scenario(saved["scenario"]["scenario_id"])
        except ReviewNotFoundError:
            scenario = None
            reasons.append("The source scenario is unavailable.")
        if scenario and scenario["version"] != saved["scenario"]["version"]:
            reasons.append("Scenario version changed after this snapshot was saved.")
        release = self.store.get_release(saved["release"]["release_id"])
        if release is None:
            reasons.append("The source release is unavailable.")
        elif release["metadata_fingerprint"] != saved["release"]["metadata_fingerprint"]:
            reasons.append("The release metadata fingerprint differs from the saved snapshot.")
        excluded = set()
        if scenario:
            with self.store.connection() as connection:
                excluded = ReviewRepository(connection).excluded_videos(scenario["scenario_id"])
        consumed, budget, proofs = 0, [MAX_SELECTED_MEDIA_BYTES], {}
        for citation in saved["citations"]:
            issues, actual = [], None
            current = self.store.get_frame(citation["release_id"], citation["frame_id"])
            if current is None or release is None:
                issues.append("The current evidence reference is unavailable.")
            else:
                if any(
                    key in citation and current.get(key) != citation[key]
                    for key in CITATION_FRAME_FIELDS
                ):
                    issues.append(
                        "Current local evidence metadata differs from the saved citation."
                    )
                if current["video_id"] in excluded:
                    issues.append("The source video is currently excluded in this scenario.")
                try:
                    proof = _fresh_digest(
                        release["dataset_root"],
                        current["relative_path"],
                        MAX_SELECTED_MEDIA_BYTES - consumed,
                        budget=budget,
                    )
                    consumed += proof["media_size_bytes"]
                    actual = proof["media_sha256"]
                    if actual != citation["media_sha256"]:
                        issues.append(
                            "Current media bytes differ from the frozen citation checksum."
                        )
                    elif not issues:
                        proofs[citation["evidence_id"]] = proof
                except (OSError, ValueError) as error:
                    issues.append(_safe_error(error))
            citation_results.append(
                {
                    "evidence_id": citation["evidence_id"],
                    "valid": not issues,
                    "reasons": _reasons(issues),
                    "saved_media_sha256": citation["media_sha256"],
                    "current_media_sha256": actual,
                }
            )
        for note in saved["included"]:
            issues, current_revision = [], None
            try:
                current = self.reviews.get_review(note["review_id"])
                current_revision = current["revision"]
                if current_revision != note["revision"]:
                    issues.append("A newer saved review revision exists.")
                elif any(key in note and current.get(key) != note[key] for key in NOTE_FIELDS):
                    issues.append("The current review content differs from the frozen revision.")
                if (
                    current["scenario_id"] != note["scenario_id"]
                    or current["release_id"] != note["release_id"]
                ):
                    issues.append("The current review belongs to another evidence workspace.")
                issues.extend(current["eligibility"]["reasons"])
            except ReviewNotFoundError:
                issues.append("The saved review is no longer available.")
            note_results.append(
                {
                    "review_id": note["review_id"],
                    "saved_revision": note["revision"],
                    "current_revision": current_revision,
                    "reasons": issues,
                }
            )
        try:
            final_scenario = self.reviews.get_scenario(saved["scenario"]["scenario_id"])
        except ReviewNotFoundError:
            final_scenario = None
        if (final_scenario["version"] if final_scenario else None) != (
            scenario["version"] if scenario else None
        ):
            reasons.append("Scenario changed during validity checking; check again.")
        scenario = final_scenario
        if release is not None:
            for citation, result in zip(saved["citations"], citation_results, strict=True):
                proof = proofs.get(citation["evidence_id"])
                if proof is not None:
                    try:
                        _recheck_proof(release["dataset_root"], citation["relative_path"], proof)
                    except (OSError, ValueError) as error:
                        result.update(
                            valid=False, reasons=[_safe_error(error)], current_media_sha256=None
                        )
        by_evidence = {item["evidence_id"]: item for item in citation_results}
        for note, result in zip(saved["included"], note_results, strict=True):
            issues = [*reasons, *result["reasons"]]
            for evidence_id in note["evidence_ids"]:
                citation_result = by_evidence.get(evidence_id)
                if citation_result is None:
                    issues.append(f"The frozen citation is unresolved: {evidence_id}")
                elif not citation_result["valid"]:
                    issues.extend(
                        f"{evidence_id}: {reason}" for reason in citation_result["reasons"]
                    )
            result.update(valid=not issues, history_only=bool(issues), reasons=_reasons(issues))
        valid = not reasons and all(item["valid"] for item in note_results + citation_results)
        all_reasons = _reasons(
            [
                *reasons,
                *(reason for item in note_results for reason in item["reasons"]),
                *(reason for item in citation_results for reason in item["reasons"]),
            ]
        )
        return {
            "brief": saved,
            "valid": valid,
            "history_only": not valid,
            "reasons": all_reasons,
            "notes": note_results,
            "citations": citation_results,
            "scenario_current_version": scenario["version"] if scenario else None,
            "checked_at": utc_now(),
        }

    def json(self, brief_id: str) -> str:
        return json_snapshot(self.get_brief(brief_id))

    def markdown(self, brief_id: str) -> str:
        return markdown_snapshot(self.get_brief(brief_id))
