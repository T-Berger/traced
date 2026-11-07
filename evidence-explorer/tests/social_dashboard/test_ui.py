"""Dashboard charts retain exact saved-record counts and bounded navigation."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from dash import Dash, dcc
from dash.development.base_component import Component
from dash.exceptions import PreventUpdate

from evidence_explorer.review.contracts import Case, EvidenceBundle, Record
from evidence_explorer.social_dashboard import ui
from evidence_explorer.social_dashboard.contracts import DashboardFilters
from evidence_explorer.social_dashboard.projection import project_dashboard


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
        return " ".join(text(child) for child in node)
    return text(getattr(node, "children", ""))


def data(count=4, *, unknown_time=True):
    records = []
    for index in range(count):
        content = f"Synthetic park plan comment {index}."
        records.append(
            Record(
                id=f"record-{index:03}",
                kind="saved_source_item",
                source_label=f"Fictional source {index}",
                text=content,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                published_at=None
                if unknown_time and index == count - 1
                else "2026-11-07T08:00:00Z",
                acquired_at="2026-11-08T09:00:00Z",
                metadata={
                    "platform": "telegram" if index % 2 else "x",
                    "channel_id": "same-channel",
                    "channel_name": "Fictional circle",
                    "author_id": "same-author",
                    "shared_urls": ["https://example.org/park-plan"],
                    "synthetic": True,
                },
            )
        )
    bundle = EvidenceBundle(
        case=Case(
            id="dashboard-ui-fixture",
            title="Synthetic UI fixture",
            question="Park plan",
            synthetic=True,
        ),
        records=records,
        fragments=[],
        runs=[],
        findings=[],
    )
    return project_dashboard(bundle).model_dump(mode="json")


def test_panel_ids_are_unique_and_registration_has_no_writes_or_queue_controls():
    panel = ui.dashboard_panel()
    nodes = list(components(panel))
    ids = [node.id for node in nodes if isinstance(getattr(node, "id", None), str)]
    assert len(ids) == len(set(ids))
    assert all(identifier.startswith(ui.PREFIX) for identifier in ids)
    assert "Topic / narrative" in text(panel) and "Public account" in text(panel)
    assert "Analyze saved text sentiment" not in text(panel)
    assert "Browse all matching records" in text(panel)
    assert not any(isinstance(node, dcc.Interval) for node in nodes)
    date_inputs = [
        node
        for node in nodes
        if getattr(node, "id", None) in {ui.PREFIX + "start", ui.PREFIX + "end"}
    ]
    assert all(node.type == "text" and node.placeholder == "YYYY-MM-DD" for node in date_inputs)
    # Registration must not touch a service constructor, database or model.
    app = Dash(__name__, suppress_callback_exceptions=True)
    app.layout = panel
    ui.register_callbacks(app, object())
    assert len(app.callback_map) == 3
    assert all("rv-session" not in key and "rv-page" not in key for key in app.callback_map)


def test_hidden_dashboard_keeps_outputs_and_does_not_capture_until_compare(monkeypatch):
    projection = data()
    reads = []

    def capture(*args):
        reads.append(args)
        return projection, {"case_fingerprint": "saved-fingerprint"}, "nav-fixture"

    monkeypatch.setattr(ui, "capture_dashboard", capture)
    app = Dash(__name__, suppress_callback_exceptions=True)
    ui.register_callbacks(app, object())

    def callback(suffix):
        return next(
            item["callback"].__wrapped__ for key, item in app.callback_map.items() if suffix in key
        )

    values = [DashboardFilters().model_dump(mode="json")[name] for name in ui.FILTER_NAMES]
    state = {"case_id": "dashboard-ui-fixture"}
    for page in ("cases", "review", "brief", None):
        with pytest.raises(PreventUpdate):
            callback("output.children")(state, page, "topics", "channel", *values)
        with pytest.raises(PreventUpdate):
            callback("drilldown.children")(
                None, None, 1, {}, state, page, "topics", "channel", *values
            )
        with pytest.raises(PreventUpdate):
            callback("selected-open.children")(
                "record-000", {}, state, page, {}, "topics", "channel", *values
            )
    assert reads == []
    _, captured, *_ = callback("output.children")(state, "compare", "topics", "channel", *values)
    assert len(reads) == 1 and captured["case_fingerprint"] == "saved-fingerprint"


def test_time_and_sentiment_counts_keep_unknowns_separate_and_points_resolve_exactly():
    projection = data()
    figure = ui.activity_figure(projection, "scope")
    assert figure.layout.hovermode == "closest"
    assert not figure.layout.xaxis2.title.text
    assert next(trace for trace in figure.data if trace.name == "Mixed").marker.color == "#754B05"
    assert sum(figure.data[0].y) == 3
    assert sum(next(trace for trace in figure.data if trace.name == "Unknown").y) == 3
    assert sum(next(trace for trace in figure.data if trace.name == "Neutral").y) == 0
    point = {"customdata": figure.data[0].customdata[0]}
    assert (
        ui.derive_selection(projection, point, "scope")
        == projection["time_buckets"][0]["record_ids"]
    )
    assert "record-003" not in ui.derive_selection(projection, point, "scope")
    assert projection["coverage"]["unknown_timestamp_records"] == 1
    with pytest.raises(ValueError, match="stale"):
        ui.derive_selection(projection, point, "new-scope")


@pytest.mark.parametrize("group_by", ["channel", "account"])
def test_accounts_lens_changes_time_series_and_resolves_platform_scoped_groups(
    group_by,
):
    projection = data()
    figure = ui.activity_figure(projection, "scope", "accounts", group_by)
    assert figure.layout.hovermode == "closest"
    activity = [trace for trace in figure.data if trace.yaxis == "y"]
    assert len(activity) == 2
    assert all(trace.showlegend is False for trace in activity)
    assert all(trace.textposition == "none" for trace in activity)
    assert all("<extra></extra>" in trace.hovertemplate for trace in activity)
    assert sum(sum(trace.y) for trace in activity) == 3
    ids = []
    for trace in activity:
        selected = ui.derive_selection(projection, {"customdata": trace.customdata[0]}, "scope")
        assert len(selected) == trace.y[0]
        ids.extend(selected)
        assert "Fictional circle" in trace.name
        assert "Fictional circle" in trace.text[0]
    assert sorted(ids) == ["record-000", "record-001", "record-002"]


def test_different_sentiment_targets_do_not_produce_an_aggregate_sentiment_chart():
    projection = data()
    projection["sentiment_comparable"] = False
    figure = ui.activity_figure(projection, "scope")
    assert all(trace.yaxis == "y" for trace in figure.data)
    assert any(
        "Different sentiment targets" in annotation.text for annotation in figure.layout.annotations
    )
    with pytest.raises(ValueError, match="cannot be combined"):
        ui.derive_selection(
            projection,
            {
                "customdata": {
                    "scope": "scope",
                    "kind": "bucket",
                    "key": projection["time_buckets"][0]["start"],
                    "sentiment": "positive",
                }
            },
            "scope",
        )


def test_relationship_graph_is_bounded_and_preserves_exact_edge_endpoints():
    projection = data(95, unknown_time=False)
    figure, rows, edges = ui.relationship_figure(projection, "scope")
    assert len(rows) == ui.GRAPH_LIMIT == 80
    assert len(figure.data[-1].customdata) == 80
    visible = {row["id"] for row in rows}
    assert all({edge["source_record_id"], edge["target_record_id"]} <= visible for edge in edges)
    if edges:
        edge = edges[0]
        assert ui.derive_selection(
            projection,
            {
                "customdata": {
                    "scope": "scope",
                    "kind": "edge",
                    "key": edge["id"],
                }
            },
            "scope",
        ) == [edge["source_record_id"], edge["target_record_id"]]
    with pytest.raises(ValueError, match="no longer available"):
        ui.derive_selection(
            projection,
            {
                "customdata": {
                    "scope": "scope",
                    "kind": "record",
                    "key": "record-094",
                }
            },
            "scope",
        )


def test_relationship_table_is_open_and_inspects_edges_outside_graph_bound():
    projection = data(95, unknown_time=False)
    panel = ui.render_dashboard(projection, "navigation-context", "scope")
    assert "Dates without bars have no saved observations; source coverage is unknown." in text(
        panel
    )
    table = next(
        node
        for node in components(panel)
        if getattr(node, "className", None) == "rv-details sd-relationship-table"
    )
    assert table.open is True
    assert "record-094" in text(table)
    actions = [node.id for node in components(table) if isinstance(getattr(node, "id", None), dict)]
    assert len(actions) == 2 * len(projection["edges"])
    assert len({action["location"] for action in actions}) == len(actions)
    assert all(action["type"] == "rv-next-action" and action["fid"] == "" for action in actions)


def test_exact_selector_opens_contributors_beyond_first_fifty_and_remount_is_neutral(
    monkeypatch,
):
    projection = data(95, unknown_time=False)
    monkeypatch.setattr(
        ui,
        "capture_dashboard",
        lambda *args: (
            projection,
            {"case_fingerprint": "saved-fingerprint"},
            "nav-fixture",
        ),
    )
    app = Dash(__name__, suppress_callback_exceptions=True)
    ui.register_callbacks(app, object())

    def callback(suffix):
        return next(
            item["callback"].__wrapped__ for key, item in app.callback_map.items() if suffix in key
        )

    filters = DashboardFilters().model_dump(mode="json")
    values = [filters[name] for name in ui.FILTER_NAMES]
    state = {"case_id": "dashboard-ui-fixture"}
    _, capture, *_ = callback("output.children")(state, "compare", "topics", "channel", *values)
    point = {"customdata": ui.activity_figure(projection, capture["scope"]).data[0].customdata[0]}
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id=ui.PREFIX + "activity-chart"))
    content, options, selected, selection = callback("drilldown.children")(
        {"points": [point]},
        None,
        0,
        capture,
        state,
        "compare",
        "topics",
        "channel",
        *values,
    )
    assert len(options) == 95 and selected is None
    assert "95 saved records" in text(content)
    opened = callback("selected-open.children")(
        "record-094",
        selection,
        state,
        "compare",
        capture,
        "topics",
        "channel",
        *values,
    )
    assert opened.id["rid"] == "record-094" and opened.id["fid"] == ""
    assert opened.id["type"] == "rv-next-action"
    cleared, options, value, selection = callback("drilldown.children")(
        None,
        None,
        0,
        capture,
        state,
        "compare",
        "topics",
        "channel",
        *values,
    )
    assert cleared.startswith("Select a time bar") and options == [] and value is selection is None
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id=ui.PREFIX + "browse"))
    _, options, _, selection = callback("drilldown.children")(
        None,
        None,
        1,
        capture,
        state,
        "compare",
        "topics",
        "channel",
        *values,
    )
    assert len(options) == 95
    assert selection["point"]["customdata"]["kind"] == "matching"
    opened = callback("selected-open.children")(
        "record-094",
        selection,
        state,
        "compare",
        capture,
        "topics",
        "channel",
        *values,
    )
    assert opened.id["rid"] == "record-094" and opened.id["fid"] == ""
