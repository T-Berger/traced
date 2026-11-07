"""Harmless specialist history remains reachable without becoming support."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from evidence_explorer.review.analysis import impact_for
from evidence_explorer.review.contracts import ObjectRef
from evidence_explorer.review.integrations.packages import import_case_package
from evidence_explorer.review.selectors import evidence_ids
from evidence_explorer.review.storage import ReviewRepository

from silent_storm.briefs import BriefService
from silent_storm.ingest import import_release
from silent_storm.media import hash_media
from silent_storm.reviews import ReviewService
from silent_storm.shared_brief import SharedBriefExporter
from silent_storm.storage import Store, canonical_json

from .conftest import DETECTION_FIELDS, FRAME_FIELDS, image_bytes, write_csv

RELEASE_ID = "history-fixture"
FRAME_ID = "history-gray"
EVIDENCE_ID = f"{RELEASE_ID}:{FRAME_ID}"
VIDEO_ID = "history-video"
REVIEW_TABLES = (
    "review_scenarios",
    "review_notes",
    "review_revisions",
    "review_evidence",
    "source_exclusions",
    "source_exclusion_events",
)


def _review_state(store: Store) -> str:
    with store.connection() as connection:
        values = {
            table: [
                dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
            ]
            for table in REVIEW_TABLES
        }
    return hashlib.sha256(canonical_json(values).encode()).hexdigest()


def build_history_case(directory: Path) -> dict:
    """Create a non-graphic disposable scenario for tests or browser acceptance."""
    directory.mkdir(parents=True, exist_ok=True)
    release_root = directory / "release"
    (release_root / "metadata").mkdir(parents=True)
    (release_root / "frames").mkdir()
    media = image_bytes(FRAME_ID)
    (release_root / "frames" / f"{FRAME_ID}.jpg").write_bytes(media)
    frame = dict.fromkeys(FRAME_FIELDS, "")
    frame.update(
        frame_id=FRAME_ID,
        file=f"{FRAME_ID}.jpg",
        clip_id="history-clip",
        video_id=VIDEO_ID,
        week="Week 7",
        image_type="Photo",
        camera="RGB",
        width_px="1",
        height_px="1",
    )
    write_csv(release_root / "metadata/frames.csv", FRAME_FIELDS, [frame])
    write_csv(release_root / "metadata/detections.csv", DETECTION_FIELDS, [])
    store = Store(directory / "specialist.sqlite")
    import_release(store, release_root, release_id=RELEASE_ID, title="Harmless history fixture")
    hash_media(store, RELEASE_ID)
    reviews = ReviewService(store)
    baseline = reviews.ensure_baseline(RELEASE_ID)
    values = dict(
        title="Harmless synthetic gray image",
        observation="A gray pixel fixture is supplied.",
        evidence_ids=[EVIDENCE_ID],
        interpretation="",
        uncertainty="Synthetic technical fixture; no real event, capture date or identity.",
    )
    original = reviews.save_review(
        baseline["scenario_id"],
        **values,
        reviewer="Fixture baseline draft actor",
        status="draft",
        rationale="Initial synthetic draft.",
    )
    original = reviews.save_review(
        baseline["scenario_id"],
        **values,
        reviewer="Fixture baseline review actor",
        status="reviewed",
        rationale="Synthetic fixture inspected for the technical test.",
        review_id=original["review_id"],
        expected_revision=1,
    )
    baseline_before = {
        "scenario": reviews.get_scenario(baseline["scenario_id"]),
        "history": reviews.review_history(original["review_id"]),
    }
    scenario = reviews.create_scenario(
        RELEASE_ID, "Harmless history scenario", "Fixture fork actor"
    )
    current = reviews.list_reviews(scenario["scenario_id"])["items"][0]
    for status, actor, reason in (
        ("draft", "Fixture draft actor", "Draft revision retained in history."),
        ("rejected", "Fixture rejection actor", "Interim wording requires correction."),
        ("reviewed", "Fixture review actor", "Current synthetic wording explicitly reviewed."),
    ):
        current = reviews.save_review(
            scenario["scenario_id"],
            **values,
            reviewer=actor,
            status=status,
            rationale=reason,
            review_id=current["review_id"],
            expected_revision=current["revision"],
        )
    preview = reviews.exclusion_preview(scenario["scenario_id"], VIDEO_ID)
    excluded = reviews.set_source_exclusion(
        scenario["scenario_id"],
        VIDEO_ID,
        excluded=True,
        reviewer="Fixture exclusion actor",
        reason="Exclude the synthetic source for a dependency check.",
        expected_version=preview["scenario_version"],
    )
    before_restore = reviews.get_review(current["review_id"])
    assert before_restore["status"] == "needs_review"
    restored = reviews.set_source_exclusion(
        scenario["scenario_id"],
        VIDEO_ID,
        excluded=False,
        reviewer="Fixture restore actor",
        reason="Restore the synthetic source without approving any statement.",
        expected_version=excluded["scenario_version"],
    )
    after_restore = reviews.get_review(current["review_id"])
    assert after_restore["status"] == "needs_review"
    assert after_restore["revision"] == before_restore["revision"]
    current = reviews.save_review(
        scenario["scenario_id"],
        **values,
        reviewer="Fixture re-review actor",
        status="reviewed",
        rationale="Explicit synthetic re-review after restoration; restoration did not approve it.",
        review_id=current["review_id"],
        expected_revision=after_restore["revision"],
    )
    assert current["revision"] == 6
    assert {
        "scenario": reviews.get_scenario(baseline["scenario_id"]),
        "history": reviews.review_history(original["review_id"]),
    } == baseline_before
    state_before = _review_state(store)
    briefs = BriefService(store, reviews)
    selection = briefs.preview(scenario["scenario_id"], [current["review_id"]])
    snapshot = briefs.create(
        scenario["scenario_id"],
        title="Harmless specialist history handoff",
        reviewer="Automated fixture author (not qualified human)",
        source_origin="synthetic",
        review_versions=selection["review_versions"],
        expected_version=selection["expected_version"],
    )
    exporter = SharedBriefExporter(store, briefs)
    bundle, package = exporter.bundle(snapshot["brief_id"]), exporter.package(snapshot["brief_id"])
    artifacts = directory / "fresh-shared-artifacts"
    reopened = import_case_package(package, artifacts)
    repository = ReviewRepository(directory / "fresh-shared.sqlite")
    repository.initialize()
    repository.import_bundle(reopened)
    stored = repository.get_bundle(reopened.case.id)
    assert stored.fingerprint() == bundle.fingerprint()
    assert _review_state(store) == state_before
    return {
        "store": store,
        "reviews": reviews,
        "briefs": briefs,
        "snapshot": snapshot,
        "bundle": stored,
        "package": package,
        "artifacts": artifacts,
        "repository": repository,
        "baseline_before": baseline_before,
        "baseline_id": baseline["scenario_id"],
        "baseline_review_id": original["review_id"],
        "review_state_before": state_before,
        "restored_version": restored["scenario_version"],
    }


def check_history_context(case: dict) -> dict:
    bundle, snapshot = case["bundle"], case["snapshot"]
    assert bundle.case.synthetic and not bundle.reviews
    assert len(bundle.findings) == 1
    finding = bundle.findings[0]
    context = finding.details["specialist_history_context"]
    assert context["label"] == "Saved specialist history and context · read only"
    assert context["read_only"] is True
    assert context["purpose"] and context["limitation"]
    record_ref = ObjectRef.model_validate(context["record_ref"])
    fragment_ref = ObjectRef.model_validate(context["fragment_ref"])
    assert record_ref.type == "record" and fragment_ref.type == "fragment"
    records = {record.id: record for record in bundle.records}
    fragments = {fragment.id: fragment for fragment in bundle.fragments}
    record, fragment = records[record_ref.id], fragments[fragment_ref.id]
    assert fragment.record_id == record.id and fragment.quote == record.text
    assert record.kind == "specialist_brief_snapshot"
    frozen = json.loads(record.text)
    assert canonical_json(frozen) == canonical_json(snapshot)
    history = sorted(frozen["review_history"]["items"], key=lambda row: row["revision"])
    statuses = [row["status"] for row in history]
    actors = [row["reviewer"] for row in history]
    assert statuses == ["reviewed", "draft", "rejected", "reviewed", "needs_review", "reviewed"]
    assert actors == [
        "Fixture fork actor",
        "Fixture draft actor",
        "Fixture rejection actor",
        "Fixture review actor",
        "Fixture exclusion actor",
        "Fixture re-review actor",
    ]
    assert frozen["review_history"]["total"] == 6
    assert frozen["review_history"]["truncated"] is False
    assert frozen["review_history"]["limit"] == 48
    events = sorted(frozen["exclusion_events"]["items"], key=lambda row: row["event_id"])
    assert [row["excluded"] for row in events] == [True, False]
    assert [row["reviewer"] for row in events] == [
        "Fixture exclusion actor",
        "Fixture restore actor",
    ]
    assert events[1]["scenario_version"] == case["restored_version"]
    assert frozen["scenario"]["version"] == case["restored_version"] + 1
    assert frozen["lineage"]["total"] == 2 and not frozen["lineage"]["truncated"]
    assert frozen["included"][0]["origin"]["reviewer"] == "Fixture baseline review actor"
    assert frozen["included"][0]["origin"]["revision"] == 2
    wire = bundle.model_dump(mode="json")
    support_before = evidence_ids(wire, finding.snapshot_id)
    without_context = copy.deepcopy(wire)
    without_context["findings"][0]["details"].pop("specialist_history_context")
    assert evidence_ids(without_context, finding.snapshot_id) == support_before
    assert record_ref.id not in support_before[0]
    assert fragment_ref.id not in support_before[1]
    assert not any(link.source.id in {record_ref.id, fragment_ref.id} for link in bundle.links)
    assert not impact_for(bundle, [record_ref.id]).affected_finding_ids
    assert frozen["included"][0]["evidence_ids"] == [EVIDENCE_ID]
    assert [item["evidence_id"] for item in frozen["citations"]] == [EVIDENCE_ID]
    assert finding.details["human_shared_disposition"] is None
    assert not [
        event
        for event in case["repository"].list_audit_events(bundle.case.id)
        if event.event_type == "finding_reviewed"
    ]
    assert _review_state(case["store"]) == case["review_state_before"]
    assert {
        "scenario": case["reviews"].get_scenario(case["baseline_id"]),
        "history": case["reviews"].review_history(case["baseline_review_id"]),
    } == case["baseline_before"]
    return {
        "status": "passed",
        "synthetic": True,
        "image_kind": "harmless 1px gray JPEG",
        "bundle_fingerprint": bundle.fingerprint(),
        "package_bytes": len(case["package"]),
        "records": len(bundle.records),
        "fragments": len(bundle.fragments),
        "findings": len(bundle.findings),
        "image_artifacts": len([record for record in bundle.records if record.artifact_ref]),
        "shared_decisions": 0,
        "history_context": context,
        "history_statuses": statuses,
        "history_actors": actors,
        "history_total": frozen["review_history"]["total"],
        "history_truncated": frozen["review_history"]["truncated"],
        "lineage_total": frozen["lineage"]["total"],
        "exclusion_sequence": [
            {"excluded": row["excluded"], "reviewer": row["reviewer"], "reason": row["reason"]}
            for row in events
        ],
        "restoration_required_explicit_rereview": True,
        "typed_context_refs_resolve": True,
        "support_projection_unchanged": True,
        "baseline_and_review_tables_unchanged_by_export": True,
        "qualified_human_review": False,
        "shared_browser_rendering_checked": False,
    }


def test_history_handoff_preserves_read_only_context_without_new_support(tmp_path):
    case = build_history_case(tmp_path / "history-case")
    check_history_context(case)


def test_history_context_remains_frozen_after_a_later_specialist_revision(tmp_path):
    case = build_history_case(tmp_path / "history-case")
    report = check_history_context(case)
    before = case["bundle"].fingerprint()
    note = case["snapshot"]["included"][0]
    case["reviews"].save_review(
        note["scenario_id"],
        title=note["title"],
        observation="A later synthetic revision.",
        reviewer="Fixture later actor",
        evidence_ids=note["evidence_ids"],
        status="draft",
        rationale="Later draft must not rewrite exported context.",
        review_id=note["review_id"],
        expected_revision=note["revision"],
    )
    reopened = case["repository"].get_bundle(case["bundle"].case.id)
    assert reopened.fingerprint() == before == report["bundle_fingerprint"]
    context = reopened.findings[0].details["specialist_history_context"]
    record = next(record for record in reopened.records if record.id == context["record_ref"]["id"])
    assert json.loads(record.text)["review_history"]["total"] == 6
