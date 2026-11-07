"""The comparison screen keeps selected sources and rendered evidence in sync."""

from types import SimpleNamespace

import pytest
from dash import dcc, html, no_update
from dash.development.base_component import Component

from evidence_explorer.review import comparison_ui
from evidence_explorer.review.comparison import compare_sources
from evidence_explorer.review.service import ReviewService
from evidence_explorer.review.storage import ReviewRepository
from evidence_explorer.review.web import create_app

from .comparison_fixture import make_comparison_bundle


def components(node):
    if isinstance(node, Component):
        yield node
        yield from components(getattr(node, "children", None))
    elif isinstance(node, (list, tuple)):
        for child in node:
            yield from components(child)


def text(node):
    if isinstance(node, str):
        return node
    if isinstance(node, (list, tuple)):
        return "".join(text(child) for child in node)
    return text(getattr(node, "children", ""))


@pytest.fixture
def comparison_desk(tmp_path):
    repository = ReviewRepository(tmp_path / "comparison-ui.sqlite3")
    repository.initialize()
    service = ReviewService(repository)
    bundle = make_comparison_bundle()
    service.import_bundle(bundle.model_dump(mode="json"))
    other = make_comparison_bundle(
        {
            "other-left": "Pine railway carried 12 passengers on Tuesday morning.",
            "other-right": "Pine railway carried 18 passengers on Tuesday morning.",
        },
        case_id="another-synthetic-case",
    )
    service.import_bundle(other.model_dump(mode="json"))
    empty = make_comparison_bundle({}, case_id="empty-synthetic-case")
    service.import_bundle(empty.model_dump(mode="json"))
    app = create_app(service)

    def callback(output):
        return next(
            item["callback"].__wrapped__ for key, item in app.callback_map.items() if output in key
        )

    return service, app, callback, bundle.case.id, other.case.id, empty.case.id


def trigger(monkeypatch, identifier):
    monkeypatch.setattr(comparison_ui, "ctx", SimpleNamespace(triggered_id=identifier))


def test_comparison_is_mounted_with_accessible_controls_and_separate_callbacks(comparison_desk):
    _, app, _, _, _, _ = comparison_desk
    nodes = list(components(app.layout()))
    by_id = {node.id: node for node in nodes if isinstance(getattr(node, "id", None), str)}
    assert {"rv-compare-summary", "rv-compare-tools", "rv-source-result"} <= by_id.keys()
    assert getattr(by_id["rv-source-result"], "aria-live") == "polite"
    assert by_id["rv-source-run"].disabled is True
    labels = {
        getattr(node, "htmlFor", None): text(node) for node in nodes if isinstance(node, html.Label)
    }
    assert labels["rv-source-left"] == "Source A"
    assert labels["rv-source-right"] == "Source B"
    assert isinstance(by_id["rv-source-case"], dcc.Store)
    assert by_id["rv-source-case"].data is None
    comparison_callbacks = [
        (key, item)
        for key, item in app.callback_map.items()
        if "callback" in item and item["callback"].__wrapped__.__module__ == comparison_ui.__name__
    ]
    assert len(comparison_callbacks) == 2
    assert not any("rv-session.data" in key for key, _ in comparison_callbacks)
    client = app.server.test_client()
    assert client.get("/_dash-layout").status_code == 200
    assert client.get("/_dash-dependencies").status_code == 200
    assert client.get("/assets/comparison.css").status_code == 200


def test_default_source_selection_is_distinct_and_does_not_replace_same_case_choices(
    comparison_desk,
):
    _, _, callback, case_id, _, empty_id = comparison_desk
    sync = callback("rv-source-left.value")
    left_options, right_options, left, right, selected_case, status = sync(
        {"case_id": case_id, "record_id": "right"}, None, None, None, None, None
    )
    assert left == "right" and right == "equivalent"
    assert selected_case == case_id and "3 saved text sources" in text(status)
    assert left in {option["value"] for option in left_options}
    assert right in {option["value"] for option in right_options}
    assert sync({"case_id": case_id, "record_id": "left"}, None, None, case_id, left, right)[
        2:5
    ] == (
        no_update,
        no_update,
        no_update,
    )
    assert sync({"case_id": empty_id}, None, None, case_id, left, right)[2:5] == (
        None,
        None,
        empty_id,
    )
    assert "No saved text" in text(sync({"case_id": empty_id}, None, None, None, None, None)[5])
    assert sync({}, None, None, case_id, left, right)[2:5] == (None, None, None)
    assert "unavailable" in text(sync({"case_id": "missing"}, None, None, None, None, None)[5])


def test_comparison_runs_only_for_explicit_current_selection_and_clears_on_case_change(
    comparison_desk, monkeypatch
):
    service, _, callback, case_id, other_id, _ = comparison_desk
    compare = callback("rv-source-result.children")
    original = service.compare_sources
    calls = []

    def record_call(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(service, "compare_sources", record_call)
    session = {"case_id": case_id}
    trigger(monkeypatch, "rv-source-right")
    ready, disabled = compare(5, "left", "right", case_id, session)
    assert disabled is False and "Choose Compare" in text(ready)
    assert calls == []
    trigger(monkeypatch, "rv-source-run")
    shown, disabled = compare(6, "left", "right", case_id, session)
    assert disabled is False and "2 possible wording" in text(shown)
    assert calls == [(case_id, "left", "right")]

    trigger(monkeypatch, "rv-session")
    cleared, disabled = compare(6, "left", "right", case_id, {"case_id": case_id, "revision": 1})
    assert disabled is False and "Selection ready" in text(cleared)
    assert cleared is not no_update and "Harbor bridge" not in text(cleared)
    stale, disabled = compare(6, "left", "right", case_id, {"case_id": other_id})
    assert disabled is True and "Harbor bridge" not in text(stale)
    assert len(calls) == 1
    trigger(monkeypatch, "rv-source-case")
    cleared, disabled = compare(6, "other-left", "other-right", other_id, {"case_id": other_id})
    assert disabled is False and "Harbor bridge" not in text(cleared)
    assert len(calls) == 1
    trigger(monkeypatch, "rv-source-run")
    fresh, disabled = compare(7, "other-left", "other-right", other_id, {"case_id": other_id})
    assert disabled is False and "Pine railway" in text(fresh)
    assert "Harbor bridge" not in text(fresh)
    assert calls[-1] == (other_id, "other-left", "other-right")


def test_batched_session_and_source_changes_clear_previous_case_result(
    comparison_desk, monkeypatch
):
    service, app, _, case_id, other_id, _ = comparison_desk
    output = next(key for key in app.callback_map if "rv-source-result.children" in key)
    client = app.server.test_client()
    original = service.compare_sources
    calls = []

    def record_call(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(service, "compare_sources", record_call)

    def invoke(selected_case, left, right, changed):
        return client.post(
            "/_dash-update-component",
            json={
                "output": output,
                "outputs": [
                    {"id": "rv-source-result", "property": "children"},
                    {"id": "rv-source-run", "property": "disabled"},
                ],
                "inputs": [
                    {"id": "rv-source-run", "property": "n_clicks", "value": 1},
                    {"id": "rv-source-left", "property": "value", "value": left},
                    {"id": "rv-source-right", "property": "value", "value": right},
                    {"id": "rv-source-case", "property": "data", "value": selected_case},
                    {"id": "rv-session", "property": "data", "value": {"case_id": selected_case}},
                ],
                "state": [],
                "changedPropIds": changed,
            },
        )

    shown = invoke(case_id, "left", "right", ["rv-source-run.n_clicks"])
    assert shown.status_code == 200
    assert "Harbor bridge" in shown.get_data(as_text=True)
    assert calls == [(case_id, "left", "right")]

    # Dash can batch the session and new selector values; the first trigger is rv-session.
    changed = invoke(
        other_id,
        "other-left",
        "other-right",
        ["rv-session.data", "rv-source-case.data", "rv-source-left.value", "rv-source-right.value"],
    )
    assert changed.status_code == 200
    response = changed.json["response"]
    assert "rv-source-result" in response, "Omitting the output would retain the old source text"
    assert "Selection ready" in response["rv-source-result"]["children"]
    assert "Harbor bridge" not in changed.get_data(as_text=True)
    assert response["rv-source-run"]["disabled"] is False
    assert calls == [(case_id, "left", "right")]


def test_invalid_pair_selection_is_disabled_and_forged_source_is_visible_error(
    comparison_desk, monkeypatch
):
    service, _, callback, case_id, _, _ = comparison_desk
    before = service.case_fingerprint(case_id)
    compare = callback("rv-source-result.children")
    trigger(monkeypatch, "rv-source-run")
    assert compare(1, "left", "left", case_id, {"case_id": case_id})[1] is True
    assert compare(1, "left", None, case_id, {"case_id": case_id})[1] is True
    error, _ = compare(1, "left", "forged-record", case_id, {"case_id": case_id})
    assert any(getattr(node, "role", None) == "alert" for node in components(error))
    assert "not in this saved investigation" in text(error)
    assert service.case_fingerprint(case_id) == before


def test_search_preserves_selected_source_without_loading_unbounded_options():
    records = [
        {"id": f"saved-{index}", "source_label": f"Article {index}", "text": "Saved source body."}
        for index in range(comparison_ui.OPTION_LIMIT + 10)
    ]
    records.append({"id": "media", "source_label": "Media", "text": None})
    selected = records[-2]["id"]
    options = comparison_ui.source_options(records, selected=selected)
    assert len(options) == comparison_ui.OPTION_LIMIT + 1
    assert options[-1]["value"] == selected
    assert len({option["value"] for option in options}) == len(options)
    assert "media" not in {option["value"] for option in options}
    searched = comparison_ui.source_options(records, search="ARTICLE 3", selected=selected)
    assert selected in {option["value"] for option in searched}
    assert all("Article 3" in option["label"] or option["value"] == selected for option in searched)
    assert comparison_ui.default_pair(records, "media") == ("saved-0", "saved-1")
    assert comparison_ui.default_pair(records[:1]) == ("saved-0", None)
    assert comparison_ui.default_pair([records[-1]]) == (None, None)


def test_source_options_and_new_case_defaults_are_delivered_atomically(comparison_desk):
    _, _, callback, case_id, other_id, _ = comparison_desk
    sync = callback("rv-source-left.options")
    left_options, right_options, left, right, retained_case, _ = sync(
        {"case_id": case_id}, "left", "right", case_id, "left", "right"
    )
    assert [option["value"] for option in left_options] == ["left"]
    assert [option["value"] for option in right_options] == ["right"]
    assert (left, right, retained_case) == (no_update, no_update, no_update)
    # Stale search terms and old IDs must not make Dash clear the new case's defaults.
    left_options, right_options, left, right, selected_case, _ = sync(
        {"case_id": other_id}, "Harbor", "Harbor", case_id, "left", "right"
    )
    assert selected_case == other_id
    assert left != right
    assert left in {option["value"] for option in left_options}
    assert right in {option["value"] for option in right_options}
    assert all(
        option["value"] in {"other-left", "other-right"} for option in left_options + right_options
    )
    assert sync({"case_id": "missing"}, None, None, case_id, "left", "right")[:2] == ([], [])
    assert sync({}, None, None, case_id, "left", "right")[:2] == ([], [])


def test_renderer_preserves_exact_text_quotes_unknown_dates_and_hidden_record_references():
    bundle = make_comparison_bundle()
    result = compare_sources(bundle, "left", "right").model_dump(mode="json")
    rendered = comparison_ui.render_comparison(result)
    nodes = list(components(rendered))
    source_texts = [node for node in nodes if isinstance(node, html.Pre)]
    assert [node.children for node in source_texts] == [
        result["left"]["text"],
        result["right"]["text"],
    ]
    assert all(node.tabIndex == 0 and getattr(node, "aria-label") for node in source_texts)
    quotes = [node for node in nodes if isinstance(node, html.Blockquote)]
    assert [text(node) for node in quotes] == [
        candidate[side]["quote"] for candidate in result["candidates"] for side in ("left", "right")
    ]
    assert {text(node) for node in nodes if isinstance(node, html.Mark)} == {"20", "35", "not"}
    details = [node for node in nodes if isinstance(node, html.Details)]
    for side in ("left", "right"):
        references = next(node for node in details if result[side]["content_hash"] in text(node))
        assert result[side]["record_id"] in text(references)
        assert "Content SHA-256" in text(references)
        assert not getattr(references, "open", False)
    assert "PublishedUnknown" in text(rendered) and "AcquiredUnknown" in text(rendered)
    panes = [node for node in nodes if getattr(node, "className", "") == "rv-source-pane"]
    assert len(panes) == 2 and all(getattr(node, "aria-label") for node in panes)
    assert any(getattr(node, "className", "") == "rv-source-panes" for node in nodes)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "javascript:alert(1)",
        "data:text/html,unsafe",
        "//example.org/story",
        "https://user:secret@example.org/story",
        "https://example.org/\nscript",
        "http://[broken",
    ],
)
def test_unsafe_source_links_are_not_rendered_as_clickable_links(url):
    result = compare_sources(make_comparison_bundle(), "left", "right").model_dump(mode="json")
    result["left"]["source_url"] = url
    result["right"]["source_url"] = url
    rendered = comparison_ui.render_comparison(result)
    assert comparison_ui.safe_source_url(url) is None
    assert not any(isinstance(node, html.A) for node in components(rendered))
    assert "No usable HTTP(S) source link" in text(rendered)


def test_valid_source_links_preserve_locator_and_markup_stays_literal_text():
    source = (
        '<script>alert("saved source text")</script>\nHarbor bridge spans 1 km across the river.'
    )
    bundle = make_comparison_bundle({"left": source, "right": source})
    result = compare_sources(bundle, "left", "right").model_dump(mode="json")
    url = "https://example.org/story?ref=saved#passage"
    result["left"]["source_url"] = url
    rendered = comparison_ui.render_comparison(result)
    nodes = list(components(rendered))
    assert not any(isinstance(node, html.Script) for node in nodes)
    assert source in [node.children for node in nodes if isinstance(node, html.Pre)]
    link = next(node for node in nodes if isinstance(node, html.A) and node.href == url)
    assert "noopener" in link.rel and "noreferrer" in link.rel
    assert "does not establish agreement" in text(rendered)


@pytest.mark.parametrize("source_origin,badge_count", [(True, 2), (False, 0), (None, 0)])
def test_only_explicit_synthetic_source_metadata_gets_a_synthetic_badge(source_origin, badge_count):
    result = compare_sources(make_comparison_bundle(), "left", "right").model_dump(mode="json")
    for side in ("left", "right"):
        result[side]["metadata"]["synthetic"] = source_origin
    rendered = comparison_ui.render_comparison(result)
    assert (
        sum(
            isinstance(node, html.Span) and text(node) == "Synthetic example"
            for node in components(rendered)
        )
        == badge_count
    )
