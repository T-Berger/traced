"""Event candidates and every chart contribution stay bound to saved evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from evidence_explorer.config import Settings
from evidence_explorer.review.contracts import EvidenceBundle, Link, Record
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.integrations.web import create_app
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.navigation import NavigationContextError, apply_navigation
from evidence_explorer.review.presentation import source_inspector
from evidence_explorer.social_dashboard.contracts import DashboardFilters
from evidence_explorer.social_dashboard.projection import project_dashboard

CASE_ID = "event-test:harbor-bridge"
RUN_ID = "event-test:analysis"
TELEGRAM_ID = "event-test:telegram-original"
REDDIT_ID = "event-test:reddit-monday"
YOUTUBE_ID = "event-test:youtube-repost"
COPY_ID = "event-test:telegram-copy"
UNDATED_ID = "event-test:youtube-undated"
UNRELATED_ID = "event-test:orchard"
AMBIGUOUS_ID = "event-test:ambiguous"
FRIDAY_CLAIM = "Harbor Bridge reopens Friday."
MONDAY_CLAIM = "Harbor Bridge reopens Monday."
EVENT_LEAD = "Harbor Bridge closed after storm damage. Harbor engineers inspect the damaged bridge."
FRIDAY_COUNT = "Harbor Bridge crews repaired 4 damaged supports."
MONDAY_COUNT = "Harbor Bridge crews repaired 8 damaged supports."
FRIDAY_TEXT = f"{EVENT_LEAD} {FRIDAY_CLAIM} {FRIDAY_COUNT}"
MONDAY_TEXT = f"{EVENT_LEAD} {MONDAY_CLAIM} {MONDAY_COUNT}"
EXPECTED_DAYS = {
    "2026-11-07": {TELEGRAM_ID, REDDIT_ID},
    "2026-11-08": {YOUTUBE_ID, COPY_ID},
}
EXPECTED_POINTS = (*EXPECTED_DAYS.values(), {UNDATED_ID})
EVENT_IDS = set().union(*EXPECTED_POINTS)


def _record(record_id, platform, label, text, published_at):
    return {
        "id": record_id,
        "kind": "social_post",
        "source_label": label,
        "text": text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "published_at": published_at,
        "acquired_at": "2026-11-10T12:00:00Z",
        "metadata": {"platform": platform, "channel_id": "public-test-feed", "synthetic": True},
    }


def _fragment(record, quote):
    start = record["text"].index(quote)
    return {
        "id": f"{record['id']}:quote",
        "record_id": record["id"],
        "selector": {"kind": "text", "start": start, "end": start + len(quote)},
        "quote": quote,
    }


def event_bundle() -> EvidenceBundle:
    """Three dated/undated timeline points and exact, opposing saved claims."""
    records = [
        _record(
            TELEGRAM_ID,
            "telegram",
            "Telegram bridge notice",
            FRIDAY_TEXT,
            "2026-11-07T09:15:00+01:00",
        ),
        _record(REDDIT_ID, "reddit", "Reddit Monday account", MONDAY_TEXT, "2026-11-07T12:00:00Z"),
        _record(
            YOUTUBE_ID,
            "youtube",
            "YouTube repair bulletin repost",
            f"{EVENT_LEAD} Repair bulletin reposted.",
            "2026-11-09T00:15:00+01:00",
        ),
        _record(
            COPY_ID, "telegram", "Telegram matching wording", FRIDAY_TEXT, "2026-11-08T11:00:00Z"
        ),
        _record(UNDATED_ID, "youtube", "YouTube undated matching wording", FRIDAY_TEXT, None),
        _record(
            UNRELATED_ID,
            "reddit",
            "Reddit orchard workshop",
            "Orchard library starts free gardening workshops for local residents.",
            "2026-11-07T09:00:00Z",
        ),
        _record(AMBIGUOUS_ID, "mastodon", "Unclassified short post", "Closed Friday.", None),
    ]
    fragments = [_fragment(records[0], FRIDAY_CLAIM), _fragment(records[1], MONDAY_CLAIM)]
    findings = [
        {
            "snapshot_id": f"event-test:{day}:v1",
            "lineage_id": f"event-test:{day}",
            "version": 1,
            "run_id": RUN_ID,
            "kind": "observation",
            "statement": claim,
        }
        for day, claim in (("friday", FRIDAY_CLAIM), ("monday", MONDAY_CLAIM))
    ]
    claims = [
        ("friday-support", fragments[0], findings[0], "supports"),
        ("friday-challenge", fragments[1], findings[0], "contradicts"),
        ("monday-support", fragments[1], findings[1], "supports"),
    ]
    links = [
        {
            "id": f"event-test:{link_id}",
            "source": {"type": "fragment", "id": fragment["id"]},
            "target": {"type": "finding", "id": finding["snapshot_id"]},
            "relation": relation,
            "basis": "Synthetic fixture declares the role of this exact saved passage.",
            "run_id": RUN_ID,
        }
        for link_id, fragment, finding, relation in claims
    ]
    links.append(
        {
            "id": "event-test:declared-repost",
            "source": {"type": "record", "id": TELEGRAM_ID},
            "target": {"type": "record", "id": YOUTUBE_ID},
            "relation": "documented_repost",
            "basis": "Synthetic fixture supplies a declared repost from the Telegram notice.",
        }
    )
    return EvidenceBundle.model_validate(
        {
            "case": {
                "id": CASE_ID,
                "title": "Synthetic Harbor Bridge event",
                "question": "Which saved posts describe the same event?",
                "synthetic": True,
            },
            "records": records,
            "fragments": fragments,
            "runs": [
                {
                    "id": RUN_ID,
                    "input_fingerprint": hashlib.sha256(
                        json.dumps(records, sort_keys=True).encode()
                    ).hexdigest(),
                    "input_manifest": [record["id"] for record in records],
                    "method": "synthetic_fixture",
                    "method_version": "1",
                    "created_at": "2026-11-10T12:00:00Z",
                }
            ],
            "findings": findings,
            "links": links,
        }
    )


def test_event_groups_posts_without_merging_copies_reposts_or_claims():
    from evidence_explorer.social_dashboard.events import cluster_events

    bundle = event_bundle()
    projected = cluster_events(bundle, project_dashboard(bundle))
    (event,) = projected.events
    assert set(event.record_ids) == EVENT_IDS
    assert set(projected.unclustered_record_ids) == {UNRELATED_ID, AMBIGUOUS_ID}
    assert [set(point.record_ids) for point in event.points] == list(EXPECTED_POINTS)
    for point, label in zip(event.points[:-1], EXPECTED_DAYS, strict=True):
        assert point.label == label
    assert event.points[-1].label == "Date unknown"

    groups = {group.kind: set(group.record_ids) for group in event.repeat_groups}
    assert groups == {
        "exact_text": {TELEGRAM_ID, COPY_ID, UNDATED_ID},
        "documented_repost": {TELEGRAM_ID, YOUTUBE_ID},
    }
    claims = {claim.statement: claim for claim in event.claims}
    assert set(claims) == {FRIDAY_CLAIM, MONDAY_CLAIM}
    assert claims[FRIDAY_CLAIM].supports == (TELEGRAM_ID,)
    assert claims[FRIDAY_CLAIM].contradicts == (REDDIT_ID,)
    assert claims[MONDAY_CLAIM].supports == (REDDIT_ID,)

    reordered = bundle.model_copy(update={"records": list(reversed(bundle.records))})
    assert cluster_events(reordered, project_dashboard(reordered)) == projected


def test_similar_posts_about_another_bridge_or_week_remain_unclustered():
    from evidence_explorer.social_dashboard.events import cluster_events

    other_bridge = _record(
        "event-test:summit-bridge",
        "youtube",
        "Summit Bridge storm damage",
        FRIDAY_TEXT.replace("Harbor", "Summit"),
        "2026-11-07T13:00:00Z",
    )
    later_closure = _record(
        "event-test:later-closure",
        "reddit",
        "Later Harbor Bridge closure",
        FRIDAY_TEXT,
        "2026-11-27T13:00:00Z",
    )
    later_closure["acquired_at"] = "2026-11-28T12:00:00Z"
    bundle = event_bundle()
    extra_records = [Record.model_validate(raw) for raw in (other_bridge, later_closure)]
    bundle = bundle.model_copy(update={"records": [*bundle.records, *extra_records]})

    projection = cluster_events(bundle, project_dashboard(bundle))

    (event,) = projection.events
    assert set(event.record_ids) == EVENT_IDS
    assert {record.id for record in extra_records} <= set(projection.unclustered_record_ids)


def test_possible_difference_keeps_exact_passages_separate_from_saved_claim_links():
    from evidence_explorer.social_dashboard.event_differences import possible_event_differences
    from evidence_explorer.social_dashboard.events import cluster_events

    bundle = event_bundle()
    (event,) = cluster_events(bundle, project_dashboard(bundle)).events
    differences = possible_event_differences(bundle, event.record_ids)
    assert differences.compared_pairs == differences.eligible_pairs == 3
    assert differences.partial_pairs == 0
    (candidate,) = differences.candidates
    assert candidate.kind == "numeric_difference"
    # The stable representative of the three identical texts is the Telegram copy.
    assert {candidate.left.record_id, candidate.right.record_id} == {COPY_ID, REDDIT_ID}
    records = {record.id: record.text for record in bundle.records}
    for passage in (candidate.left, candidate.right):
        assert records[passage.record_id][passage.start : passage.end] == passage.quote
    assert {candidate.left.quote, candidate.right.quote} == {FRIDAY_COUNT, MONDAY_COUNT}
    (saved_claim,) = [claim for claim in event.claims if claim.contradicts]
    assert saved_claim.statement == FRIDAY_CLAIM
    assert saved_claim.supports == (TELEGRAM_ID,)
    assert saved_claim.contradicts == (REDDIT_ID,)


def test_wording_difference_reports_partially_examined_saved_text():
    from evidence_explorer.social_dashboard.event_differences import possible_event_differences
    from evidence_explorer.social_dashboard.events import cluster_events
    from evidence_explorer.social_dashboard.events_ui import render_event_clusters

    fixture = event_bundle()
    prefix = "Harbor crews posted an archived bridge maintenance note. " * 800
    records = []
    for record, suffix in zip(fixture.records[:2], (FRIDAY_COUNT, MONDAY_COUNT), strict=True):
        text = prefix + suffix
        records.append(
            record.model_copy(
                update={"text": text, "content_hash": hashlib.sha256(text.encode()).hexdigest()}
            )
        )
    bundle = EvidenceBundle(case=fixture.case, records=records, fragments=[], runs=[], findings=[])
    dashboard = project_dashboard(bundle)
    projection = cluster_events(bundle, dashboard)
    (event,) = projection.events
    differences = possible_event_differences(bundle, event.record_ids)
    assert (
        differences.eligible_pairs == differences.compared_pairs == differences.partial_pairs == 1
    )
    assert not differences.candidates
    assert not differences.candidates_truncated
    by_id = dict(differences.source_coverage)
    for record in records:
        checked = by_id[record.id]
        assert checked.truncated
        assert checked.examined_chars < checked.total_chars == len(record.text)
        assert checked.examined_sentences == checked.sentence_limit
    rendered = render_event_clusters(projection, dashboard, "fixture", {event.id: differences})
    assert "Source limits left text unexamined in 1 of the selected pairs." in _text(rendered)
    assert "No differences flagged in the examined text." in _text(rendered)


def test_excluded_record_leaves_timeline_and_claim_roles():
    from evidence_explorer.social_dashboard.events import cluster_events

    bundle = event_bundle()
    projected = cluster_events(bundle, project_dashboard(bundle, excluded_record_ids={REDDIT_ID}))
    (event,) = projected.events
    assert set(event.record_ids) == EVENT_IDS - {REDDIT_ID}
    assert REDDIT_ID not in projected.unclustered_record_ids
    assert REDDIT_ID not in {rid for point in event.points for rid in point.record_ids}
    assert REDDIT_ID not in {
        rid for claim in event.claims for rid in (*claim.supports, *claim.contradicts)
    }


def test_reposts_cannot_bridge_through_an_excluded_or_out_of_event_record():
    from evidence_explorer.social_dashboard.events import cluster_events

    bundle = event_bundle()
    chain = [
        Link.model_validate(
            {
                "id": f"event-test:repost-chain-{index}",
                "source": {"type": "record", "id": source},
                "target": {"type": "record", "id": target},
                "relation": "documented_repost",
                "basis": "Saved link through a separate post.",
            }
        )
        for index, (source, target) in enumerate(
            ((TELEGRAM_ID, UNRELATED_ID), (UNRELATED_ID, AMBIGUOUS_ID))
        )
    ]
    bundle = bundle.model_copy(update={"links": [*bundle.links, *chain]})
    projection = cluster_events(bundle, project_dashboard(bundle))
    assert AMBIGUOUS_ID in projection.unclustered_record_ids
    excluded = project_dashboard(bundle, excluded_record_ids={UNRELATED_ID})
    (event,) = cluster_events(bundle, excluded).events
    assert set(event.record_ids) == EVENT_IDS
    assert {group.id for group in event.repeat_groups}.isdisjoint(link.id for link in chain)


@pytest.mark.parametrize(
    "filters", [DashboardFilters(time_basis="acquisition"), DashboardFilters(bucket="week")]
)
def test_event_points_require_publication_day_buckets(filters):
    from evidence_explorer.social_dashboard.events import cluster_events

    bundle = event_bundle()
    with pytest.raises(ValueError, match="saved publication days"):
        cluster_events(bundle, project_dashboard(bundle, filters=filters))


def _wire_components(value):
    if isinstance(value, dict) and "props" in value:
        yield value
        yield from _wire_components(value["props"].get("children"))
    elif isinstance(value, list):
        for child in value:
            yield from _wire_components(child)


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "".join(_text(child) for child in value)
    if isinstance(value, dict):
        return _text(value["props"].get("children"))
    return _text(getattr(value, "children", None)) if value is not None else ""


def _next_actions(value):
    return [
        action
        for child in _wire_components(value)
        if isinstance(action := child["props"].get("id"), dict)
        and action.get("type") == "rv-next-action"
    ]


def _database_dump(path):
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        return tuple(db.iterdump())


def _render_callback(app, state):
    inputs = [
        {"id": "rv-page", "property": "value", "value": "compare"},
        {"id": "rv-session", "property": "data", "value": state},
    ]
    response = app.server.test_client().post(
        "/_dash-update-component",
        json={
            "output": "event-clusters-output.children",
            "outputs": {"id": "event-clusters-output", "property": "children"},
            "inputs": inputs,
            "state": [],
            "changedPropIds": ["rv-page.value"],
        },
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["response"]["event-clusters-output"]["children"]


def test_each_timeline_point_opens_exact_posts_and_navigation_is_read_only(tmp_path):
    bundle = event_bundle()
    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    assert service.import_bundle(bundle.model_dump(mode="json")) == CASE_ID
    state = session_for_case(service, CASE_ID)
    app = create_app(service)
    layout = app.server.test_client().get("/_dash-layout").get_json()
    compare_tools = next(
        node for node in _wire_components(layout) if node["props"].get("id") == "rv-compare-tools"
    )
    assert (
        sum(
            node["props"].get("id") == "event-clusters-panel"
            for node in _wire_components(compare_tools)
        )
        == 1
    )
    before = _database_dump(settings.db_path)
    rendered = _render_callback(app, state)
    summaries = {_text(node) for node in _wire_components(rendered) if node["type"] == "Summary"}
    assert {
        "Same normalized text · 3 saved",
        "Documented repost link · 2 saved",
        FRIDAY_CLAIM,
        MONDAY_CLAIM,
        "Possible wording differences (1)",
        "Unclustered saved records (2)",
    } <= summaries
    all_actions = _next_actions(rendered)
    assert {action["rid"] for action in all_actions} == EVENT_IDS | {UNRELATED_ID, AMBIGUOUS_ID}
    assert len({json.dumps(action, sort_keys=True) for action in all_actions}) == len(all_actions)
    points = [
        node
        for node in _wire_components(rendered)
        if node["type"] == "Details"
        and "event-clusters-point" in node["props"].get("className", "").split()
    ]
    assert len(points) == len(EXPECTED_POINTS)
    records = {record.id: record for record in bundle.records}
    for point, exact_ids in zip(points, EXPECTED_POINTS, strict=True):
        actions = _next_actions(point)
        assert {action["rid"] for action in actions} == exact_ids
        assert len(actions) == len(exact_ids)
        for action in actions:
            assert action["action"] == "source"
            selected = apply_navigation(service, state, action)
            assert selected["record_id"] == action["rid"]
            assert selected["navigate_to"] == "review"
            assert selected["navigate_target"] == "rv-source"
            record = records[action["rid"]]
            inspector = source_inspector(
                service.get_case(CASE_ID), selected["record_id"], selected["finding_id"], set()
            )
            assert record.source_label in _text(inspector)
            assert record.text in _text(inspector)
            with pytest.raises(NavigationContextError, match="version changed"):
                apply_navigation(service, {**state, "scenario_version": 2}, action)
    assert _database_dump(settings.db_path) == before
