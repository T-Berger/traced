"""Review desk regressions: read-only page loads and finding-scoped evidence."""

import sqlite3
from types import SimpleNamespace

import pytest
from dash import html, no_update
from dash.development.base_component import Component

from evidence_explorer.review import web
from evidence_explorer.review.presentation import highlight_text, source_inspector
from evidence_explorer.review.selectors import evidence_ids, initial_selection, source_for_finding
from evidence_explorer.review.service import ReviewService
from evidence_explorer.review.storage import ReviewRepository


def components(node):
    if isinstance(node, Component):
        yield node
        yield from components(getattr(node, "children", None))
    elif isinstance(node, (list, tuple)):
        for child in node:
            yield from components(child)


@pytest.fixture
def desk(tmp_path):
    repository = ReviewRepository(tmp_path / "review.sqlite3")
    repository.initialize()
    service = ReviewService(repository)
    service.seed_demo()
    return repository, service, web.create_app(service, prefix="/review/")


def session(app):
    return next(
        node.data for node in components(app.layout()) if getattr(node, "id", None) == "rv-session"
    )


def action(app, monkeypatch, state, trigger, **values):
    definition = next(
        item for key, item in app.callback_map.items() if "rv-action-message.children" in key
    )
    intent = (
        {"event_id": "test-action", "trigger": trigger, "clicks": 1}
        if isinstance(trigger, dict)
        else None
    )
    callback_trigger = "rv-click-intent" if intent else trigger
    property_name = "data" if intent else "tapNodeData" if trigger == "rv-graph" else "n_clicks"
    monkeypatch.setattr(
        web,
        "ctx",
        SimpleNamespace(
            triggered_id=callback_trigger,
            triggered=[{"prop_id": f"{callback_trigger}.{property_name}"}],
        ),
    )
    inputs = {
        "rv-click-intent": intent,
        "rv-case": values.get("case_id", state["case_id"]),
        "rv-graph": values.get("node"),
        "rv-scenario": state.get("scenario_id") or "baseline",
        "rv-session": state,
        "rv-exclude-reason": values.get(
            "exclude_reason", "Source is insufficient for this conclusion"
        ),
        "rv-review-reason": values.get("review_reason", "Use with the documented limits"),
        "rv-challenge-mode": values.get("challenge_mode", "demo"),
    }
    parameters = []
    for item in [*definition["inputs"], *definition["state"]]:
        identifier = item["id"]
        if identifier.startswith("{"):
            value = [1] if "rv-finding" in identifier or "rv-record" in identifier else [0]
        else:
            value = inputs.get(identifier, 0)
        parameters.append(value)
    return definition["callback"].__wrapped__(*parameters)[:2]


def test_page_reads_do_not_create_scenarios_or_audit_events(desk):
    repository, service, app = desk
    case_id = session(app)["case_id"]
    before = service.repository.list_events(case_id)
    client = app.server.test_client()
    for _ in range(3):
        assert client.get("/review/").status_code == 200
        assert client.get("/review/_dash-layout").status_code == 200
        assert session(app)["scenario_id"] is None
    assert client.get("/review/health").get_json()["status"] == "ok"
    assert service.repository.list_events(case_id) == before
    with sqlite3.connect(repository.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_scenarios").fetchone()[0] == 0


@pytest.mark.parametrize("trigger", ["rv-challenge", "rv-use", "rv-toggle-source"])
def test_first_action_creates_and_retains_a_scenario(desk, monkeypatch, trigger):
    repository, service, app = desk
    state, _ = action(app, monkeypatch, session(app), trigger)
    assert state is not no_update
    assert state["scenario_id"]
    assert state["scenario_version"] >= 1
    assert service.view(state["case_id"], state["scenario_id"])["scenario"]
    next_state, _ = action(app, monkeypatch, state, "rv-use")
    assert next_state["scenario_id"] == state["scenario_id"]
    with sqlite3.connect(repository.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_scenarios").fetchone()[0] == 1


def test_graph_and_queue_finding_selection_choose_the_same_source(desk, monkeypatch):
    _, service, app = desk
    stored = session(app)
    bundle = service.get_case(stored["case_id"])
    finding = next(item for item in bundle["findings"] if item["kind"] == "observation")
    fid = finding["snapshot_id"]
    graph_state, _ = action(
        app, monkeypatch, stored, "rv-graph", node={"kind": "finding", "ref_id": fid}
    )
    queue_state, _ = action(app, monkeypatch, stored, {"type": "rv-finding", "fid": fid})
    assert graph_state["record_id"] == queue_state["record_id"] == source_for_finding(bundle, fid)
    assert graph_state["job_id"] is None


def test_source_timeline_selection_opens_record_in_review(desk, monkeypatch):
    _, service, app = desk
    stored = session(app)
    record_id = service.get_case(stored["case_id"])["records"][0]["id"]
    state, _ = action(
        app,
        monkeypatch,
        stored,
        {"type": "rv-record", "rid": record_id, "location": "source-timeline-0"},
    )
    assert state["record_id"] == record_id
    assert state["navigate_to"] == "review"


def test_unlinked_finding_clears_source_selection(desk, monkeypatch):
    _, service, app = desk
    stored = session(app)
    bundle = service.get_case(stored["case_id"])
    bundle["findings"].append(
        {
            **bundle["findings"][0],
            "snapshot_id": "no-direct-evidence",
            "lineage_id": "unlinked-finding",
        }
    )
    service.import_bundle(bundle)
    state, _ = action(
        app,
        monkeypatch,
        stored,
        "rv-graph",
        node={"kind": "finding", "ref_id": "no-direct-evidence"},
    )
    assert state["record_id"] is None


def test_review_selection_and_case_picker_persist_in_browser_session(desk):
    _, _, app = desk
    nodes = {
        getattr(node, "id", None): node
        for node in components(app.layout())
        if isinstance(getattr(node, "id", None), str)
    }
    assert nodes["rv-session"].storage_type == "session"
    assert nodes["rv-case"].persistence is True
    assert nodes["rv-case"].persistence_type == "session"


def test_evidence_query_preserves_reference_types():
    bundle = {
        "links": [
            {
                "source": {"type": "record", "id": "source"},
                "target": {"type": "record", "id": "same-id"},
                "relation": "same_content",
            }
        ]
    }
    assert evidence_ids(bundle, "same-id") == (set(), set())


def test_initial_selection_uses_data_without_a_fixture_id_override():
    bundle = {
        "findings": [
            {"snapshot_id": "first-inference", "kind": "inference"},
            {"snapshot_id": "harbor:c3-v1", "kind": "inference"},
        ]
    }
    assert initial_selection(bundle) == ("first-inference", None)


def test_source_does_not_highlight_fragments_from_another_finding():
    bundle = {
        "records": [{"id": "r", "text": "An unrelated quotation"}],
        "fragments": [
            {
                "id": "other-fragment",
                "record_id": "r",
                "quote": "unrelated",
                "selector": {"kind": "text", "start": 3, "end": 12},
            }
        ],
        "links": [
            {
                "source": {"type": "fragment", "id": "other-fragment"},
                "target": {"type": "finding", "id": "another-finding"},
            }
        ],
    }
    inspector = source_inspector(bundle, "r", "selected-finding", set())
    assert not any(isinstance(node, html.Mark) for node in components(inspector))


@pytest.mark.parametrize("cited", [True, False])
def test_long_source_keeps_exact_passage_and_full_record_available(cited):
    quote = "The recorded observation."
    prefix = "Saved context. " * 50
    text = prefix + quote + " End of the saved record."
    timestamp = "2026-10-14T08:08:03Z"
    record = {
        "id": "record-with-an-audit-id",
        "source_label": "Saved publisher",
        "text": text,
        "published_at": timestamp,
        "content_hash": "saved-content-hash",
        "metadata": {"synthetic": False},
    }
    fragment = {
        "id": "exact-fragment",
        "record_id": record["id"],
        "quote": quote,
        "selector": {"kind": "text", "start": len(prefix), "end": len(prefix) + len(quote)},
    }
    bundle = {
        "records": [record],
        "fragments": [fragment],
        "links": [
            {
                "source": {"type": "fragment", "id": fragment["id"]},
                "target": {"type": "finding", "id": "finding"},
                "relation": "supports",
            }
        ],
    }
    inspector = source_inspector(bundle, record["id"], "finding" if cited else None, set())
    visible = [child for child in inspector.children if not isinstance(child, html.Details)]
    assert "Published 14 Oct 2026" in repr(visible)
    assert "Collected source" in repr(visible)
    assert timestamp not in repr(visible)
    assert record["id"] not in repr(visible)
    visible_marks = [node.children for node in components(visible) if isinstance(node, html.Mark)]
    assert visible_marks == ([quote] if cited else [])
    assert ("Exact saved passages" if cited else "Opening of saved record") in repr(visible)
    full, audit = [child for child in inspector.children if isinstance(child, html.Details)]
    assert not getattr(full, "open", False) and not getattr(audit, "open", False)
    assert full.children[0].children == "Read full saved record"
    assert (
        "".join(
            child.children if isinstance(child, html.Mark) else child
            for child in full.children[1].children
        )
        == text
    )
    assert timestamp in repr(audit) and record["id"] in repr(audit)
    assert record["content_hash"] in repr(audit)
    assert (fragment["id"] in repr(audit)) is cited


def test_highlighting_requires_exact_text_selectors_and_merges_overlaps():
    fragments = [
        {"selector": {"kind": "text", "start": 0, "end": 3}, "quote": "abc"},
        {"selector": {"kind": "text", "start": 2, "end": 5}, "quote": "cde"},
        {"selector": {"kind": "text", "start": 5, "end": 6}, "quote": "wrong"},
    ]
    children, exact = highlight_text("abcdef", fragments)
    assert exact
    assert [node.children for node in children if isinstance(node, html.Mark)] == ["abcde"]
    invalid = [
        {"selector": {"kind": "whole_record", "start": 0, "end": 1}, "quote": "a"},
        {"selector": {"kind": "text", "start": False, "end": True}, "quote": "a"},
    ]
    assert highlight_text("abcdef", invalid) == (["abcdef"], False)


def test_selected_finding_renders_with_its_saved_evidence(desk):
    _, service, app = desk
    state = session(app)
    render = next(
        item["callback"].__wrapped__
        for key, item in app.callback_map.items()
        if "rv-findings.children" in key
    )
    result = render(state)
    bundle = service.get_case(state["case_id"])
    assert result[1] == str(len(bundle["findings"]))
    assert any(isinstance(node, html.Mark) for node in components(result[4]))
    assert result[-1] is False
