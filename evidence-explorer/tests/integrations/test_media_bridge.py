"""Media imports preserve unknown context and evidence identity across round trips."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.integrations.media import (
    build_media_demo_bundle,
    build_reviewed_media_bundle,
    media_artifact_root,
)

IMPORTED_AT = "2026-11-06T08:00:00+01:00"


def _row(**changes):
    return {
        "frame_id": "frame-a",
        "file": "frames/card-a.svg",
        "clip_id": "clip-a",
        "video_id": "video-a",
        "week": "11",
        "side": "Blue Force",
        **changes,
    }


def _build(rows, **changes):
    return build_reviewed_media_bundle(
        rows,
        artifact_root=media_artifact_root(),
        release_id="test-release",
        imported_at=IMPORTED_AT,
        **changes,
    )


def test_demo_media_keeps_capture_context_and_depicted_affiliation_unknown():
    bundle = build_media_demo_bundle()
    assert bundle.case.synthetic is True
    frames = [record for record in bundle.records if record.kind == "supplied_frame"]
    assert len(frames) == 3
    assert {record.metadata["first_posted_exercise_week"] for record in frames} == {11, 12}
    assert {record.metadata["posting_channel_affiliation"] for record in frames} == {
        "Blue Force",
        "Rusviet",
    }
    for record in frames:
        assert record.source_url is None
        assert record.published_at is None
        assert record.metadata["depicted_side"] is None
        assert record.metadata["capture_time"] is None
        assert record.metadata["event_time"] is None
        assert record.metadata["location"] is None
    assert "event counts" in " ".join(bundle.case.limitations)


def test_demo_duplicate_link_uses_actual_saved_bytes_without_claiming_an_origin():
    bundle = build_media_demo_bundle()
    records = {record.id: record for record in bundle.records}
    repeated_links = [link for link in bundle.links if link.relation == "same_content"]
    assert len(repeated_links) == 1
    duplicate_link = repeated_links[0]
    source = records[duplicate_link.source.id]
    target = records[duplicate_link.target.id]
    assert source.artifact_ref != target.artifact_ref
    for record in (source, target):
        data = (media_artifact_root() / record.artifact_ref).read_bytes()
        assert record.content_hash == hashlib.sha256(data).hexdigest()
    assert source.content_hash == target.content_hash
    assert "origin is unknown" in duplicate_link.basis
    assert not [link for link in bundle.links if link.relation == "documented_repost"]


def test_frame_clip_video_relationships_resolve_without_becoming_event_links():
    bundle = build_media_demo_bundle()
    records = {record.id: record for record in bundle.records}
    frames = [record for record in bundle.records if record.kind == "supplied_frame"]
    for frame in frames:
        clip_link = next(
            link
            for link in bundle.links
            if link.source.id == frame.id and link.relation == "part_of"
        )
        clip = records[clip_link.target.id]
        assert clip.kind == "supplied_clip"
        video_link = next(
            link
            for link in bundle.links
            if link.source.id == clip.id and link.relation == "part_of"
        )
        assert records[video_link.target.id].kind == "supplied_source_video"
    assert not [record for record in bundle.records if record.kind == "event"]
    assert bundle.case.coverage["selected_frames"] == 3
    assert bundle.case.coverage["selected_clips"] == 2
    assert bundle.case.coverage["selected_source_videos"] == 2
    assert bundle.case.coverage["full_release_processed"] is False


def test_reviewed_unsupported_event_inference_has_dependencies_and_withheld_decision():
    bundle = build_media_demo_bundle()
    inference = next(finding for finding in bundle.findings if finding.kind == "inference")
    premises = [
        link
        for link in bundle.links
        if link.target.id == inference.snapshot_id and link.relation == "depends_on"
    ]
    assert len(premises) == 2
    assert bundle.reviews[0].finding_snapshot_id == inference.snapshot_id
    assert bundle.reviews[0].disposition == "withhold"
    assert bundle.reviews[0].origin == "synthetic_review_fixture"
    assert "does not establish" in bundle.reviews[0].reason
    restored = EvidenceBundle.model_validate_json(bundle.model_dump_json())
    assert restored.fingerprint() == bundle.fingerprint()
    assert restored.reviews[0].evidence_refs == bundle.reviews[0].evidence_refs


def test_media_fixture_drives_existing_challenge_and_dependency_review_components():
    from evidence_explorer.review.analysis import challenge_finding, compute_impact

    bundle = build_media_demo_bundle()
    original = bundle.fingerprint()
    inference = next(finding for finding in bundle.findings if finding.kind == "inference")
    result = challenge_finding(bundle, inference.snapshot_id, mode="demo")
    assert result["findings"][0]["finding_type"] == "unsupported_inference"
    assert result["findings"][0]["fragment_ids"]
    frame = next(record for record in bundle.records if record.kind == "supplied_frame")
    impact = compute_impact(bundle, [frame.id])
    assert inference.snapshot_id in impact["affected_finding_ids"]
    assert frame.id not in impact["remaining_evidence"][inference.snapshot_id]
    assert bundle.fingerprint() == original


def test_repeated_build_is_stable_and_row_order_does_not_change_identity():
    assert build_media_demo_bundle().fingerprint() == build_media_demo_bundle().fingerprint()
    rows = [_row(), _row(frame_id="frame-b", file="frames/card-b.svg")]
    first = _build(rows)
    second = _build(reversed(rows))
    assert first.fingerprint() == second.fingerprint()


@pytest.mark.parametrize("filename", ["../card.svg", "/tmp/card.svg", "frames\\card.svg"])
def test_media_paths_cannot_leave_the_explicit_trusted_root(filename):
    with pytest.raises(ValueError, match="relative"):
        _build([_row(file=filename)])


def test_media_file_symlink_cannot_expose_another_directory(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.svg"
    outside.write_text("<svg/>")
    (root / "linked.svg").symlink_to(outside)
    with pytest.raises(ValueError, match="symbolic links"):
        build_reviewed_media_bundle(
            [_row(file="linked.svg")],
            artifact_root=root,
            release_id="test",
            imported_at=IMPORTED_AT,
        )


def test_import_is_bounded_before_reading_extra_frames():
    with pytest.raises(ValueError, match="at most 1"):
        _build([_row(), _row(frame_id="other")], max_frames=1)


@pytest.mark.parametrize("week", [None, "", "not a week", -1, True])
def test_missing_or_invalid_exercise_week_is_rejected_instead_of_inferred(week):
    with pytest.raises(ValueError, match="exercise-week"):
        _build([_row(week=week)])


def test_conflicting_clip_video_mapping_and_duplicate_frame_ids_are_rejected():
    with pytest.raises(ValueError, match="different source videos"):
        _build([_row(), _row(frame_id="other", video_id="other-video")])
    with pytest.raises(ValueError, match="Duplicate frame"):
        _build([_row(), _row()])


def test_raw_unverified_detections_do_not_become_reviewed_image_findings():
    bundle = _build([_row(detections=[{"class": "unverified", "confidence": 0.7}])])
    assert len(bundle.findings) == 1
    assert bundle.findings[0].kind == "observation"
    assert "subset contains" in bundle.findings[0].statement
    assert bundle.reviews == []
    assert any('"confidence":0.7' in (record.text or "") for record in bundle.records)


def test_timestamp_needs_timezone_and_files_must_exist():
    with pytest.raises(ValueError, match="timezone"):
        build_reviewed_media_bundle(
            [_row()],
            artifact_root=media_artifact_root(),
            release_id="test",
            imported_at="2026-11-06T08:00:00",
        )
    with pytest.raises(ValueError, match="does not exist"):
        _build([_row(file="frames/missing.svg")])


def test_demo_artifacts_are_relative_clean_svg_files():
    bundle = build_media_demo_bundle()
    for record in bundle.records:
        if record.artifact_ref:
            assert not Path(record.artifact_ref).is_absolute()
            source = (media_artifact_root() / record.artifact_ref).read_text()
            assert "SYNTHETIC MEDIA FIXTURE" in source
            assert "<script" not in source.lower()
            assert "href=" not in source.lower()
