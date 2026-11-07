"""Saved dashboard navigation is read-only and retains exact native evidence context."""

from __future__ import annotations

import hashlib
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from dash import Dash, dcc, html
from dash.development.base_component import Component
from dash.exceptions import PreventUpdate

from evidence_explorer.config import Settings
from evidence_explorer.models_client import OllamaClient
from evidence_explorer.review.contracts import Case, EvidenceBundle, Finding, Fragment, Record, Run
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.integrations.packages import export_case_package, read_case_package
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.navigation import apply_navigation
from evidence_explorer.social_dashboard.projection import project_dashboard

NOW = "2026-11-07T15:00:00Z"


def source_fixture() -> EvidenceBundle:
    descriptions = [
        ("r-positive", "The park plan is welcome.", "telegram", "2026-11-06T23:30:00-02:00"),
        ("r-negative", "The park plan is disappointing.", "telegram", None),
        ("r-other-platform", "The park plan has opened.", "x", "2026-11-07T10:00:00Z"),
        ("r-naive", "Music has returned.", "news", "2026-11-07T11:00:00"),
        ("r-date-only", "The park timetable is saved.", "telegram", "2026-11-07"),
        ("r-outside", "The park was discussed earlier.", "telegram", "2026-11-01T12:00:00Z"),
    ]
    records = [
        Record(
            id=rid,
            kind="saved_source_item",
            source_label=f"Synthetic {rid}",
            text=text,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            source_url=f"https://example.org/{rid}",
            published_at=published,
            acquired_at="2026-11-08T10:00:00Z",
            metadata={
                "platform": platform,
                "channel_id": "shared-channel" if platform != "news" else None,
                "channel_name": "Shared display name" if platform != "news" else None,
                "author_id": "same-author" if platform != "news" else None,
                "native_id": rid,
                "synthetic": True,
            },
        )
        for rid, text, platform, published in descriptions
    ]
    fragments = [
        Fragment(
            id=record.id + ":quote",
            record_id=record.id,
            selector={"kind": "text", "start": 0, "end": len(record.text)},
            quote=record.text,
        )
        for record in records[:2]
    ]
    run = Run(
        id="saved-sentiment-run",
        input_fingerprint="fixture-input",
        input_manifest=[record.id for record in records],
        method="saved synthetic sentiment annotation",
        method_version="fixture-v1",
        created_at=NOW,
        configuration={"model": "fixture-model@exact", "prompt_version": "fixture-prompt-v1"},
    )
    findings = [
        Finding(
            snapshot_id=record.id + ":sentiment",
            lineage_id=record.id + ":sentiment-lineage",
            version=1,
            run_id=run.id,
            kind="inference",
            statement=f"Synthetic saved {label} annotation about the park plan.",
            details={
                "domain": "social_sentiment",
                "annotation": {
                    "record_id": record.id,
                    "content_hash": record.content_hash,
                    "label": label,
                    "target": "park plan",
                    "model": "fixture-model@exact",
                    "prompt_version": "fixture-prompt-v1",
                    "fragment_ids": [record.id + ":quote"],
                },
            },
        )
        for record, label in zip(records[:2], ("positive", "negative"), strict=True)
    ]
    return EvidenceBundle(
        case=Case(
            id="saved-dashboard-case",
            title="Synthetic dashboard case",
            question="Saved sample",
            synthetic=True,
        ),
        records=records,
        fragments=fragments,
        runs=[run],
        findings=findings,
    )


def stored_rows(repository):
    with sqlite3.connect(repository.db_path) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        result = {}
        for (name,) in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            rows = connection.execute("SELECT * FROM " + quoted).fetchall()
            result[name] = hashlib.sha256(repr(sorted(rows, key=repr)).encode()).hexdigest()
        return result


@pytest.fixture
def desk(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, ollama_url="http://127.0.0.1:1")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    repository.import_bundle(source_fixture())
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")

    def forbidden(*args, **kwargs):
        raise AssertionError("The saved dashboard must not fetch, infer, queue or persist.")

    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(OllamaClient, "__init__", forbidden)
    monkeypatch.setattr(WorkspaceRepository, "initialize", forbidden)
    for name in (
        "collect_source",
        "add_collection_source",
        "set_collection_enabled",
        "import_bundle",
        "import_package",
        "queue_recomputation",
        "recompute",
        "challenge",
        "exclude",
        "review",
    ):
        monkeypatch.setattr(service, name, forbidden)
    return service


def nodes(node):
    if isinstance(node, Component):
        yield node
        yield from nodes(getattr(node, "children", None))
    elif isinstance(node, (tuple, list)):
        for child in node:
            yield from nodes(child)


def text(node):
    if isinstance(node, str):
        return node
    if isinstance(node, (tuple, list)):
        return " ".join(text(child) for child in node)
    return text(getattr(node, "children", ""))


@pytest.fixture
def dashboard(desk):
    from evidence_explorer.social_dashboard import ui

    before = stored_rows(desk.repository)
    app = Dash(__name__, suppress_callback_exceptions=True)
    app.layout = html.Div(
        [dcc.Store(id="rv-session"), dcc.Store(id="rv-page", data="compare"), ui.dashboard_panel()]
    )
    ui.register_callbacks(app, desk)
    assert stored_rows(desk.repository) == before

    def callback(suffix):
        return next(
            item["callback"].__wrapped__
            for key, item in app.callback_map.items()
            if ui.PREFIX + suffix in key
        )

    return desk, ui, app, callback


def filter_values(ui, **changes):
    from evidence_explorer.social_dashboard.contracts import DashboardFilters

    filters = DashboardFilters(**changes).model_dump(mode="json")
    return [filters[name] for name in ui.FILTER_NAMES]


@pytest.mark.parametrize("situation", ["initial_review", "no_selected_case", "invalid_filter"])
def test_absent_charts_have_optional_dependencies_and_clear_selection_without_reads(
    dashboard, monkeypatch, situation
):
    service, ui, app, callback = dashboard
    state = session_for_case(service, "saved-dashboard-case")
    page = "review" if situation == "initial_review" else "compare"
    values = filter_values(ui)
    if situation == "no_selected_case":
        state = {}
    elif situation == "invalid_filter":
        values[ui.FILTER_NAMES.index("start")] = "not-a-date"
    if page == "compare":
        output, capture, *_ = callback("output.children")(state, page, "topics", "channel", *values)
        assert capture is None
        next(
            node for node in nodes(app.layout) if getattr(node, "id", None) == ui.PREFIX + "output"
        ).children = output

    mounted = {node.id for node in nodes(app.layout) if isinstance(getattr(node, "id", None), str)}
    client = app.server.test_client()
    dependencies = client.get("/_dash-dependencies").get_json()
    absent = [
        item
        for dependency in dependencies
        for item in dependency["inputs"]
        if item["id"] not in mounted
    ]
    assert {item["id"] for item in absent} == {
        ui.PREFIX + "activity-chart",
        ui.PREFIX + "relationship-chart",
    }
    layout_json = client.get("/_dash-layout").get_data(as_text=True)
    assert all(item["id"] not in layout_json for item in absent)
    # The browser renderer, not just the server callback, must accept the missing inputs.
    assert all(item.get("allow_optional") is True for item in absent)
    assert all(
        not item.get("allow_optional", False)
        for dependency in dependencies
        for item in dependency["inputs"]
        if item["id"] in mounted
    )

    before = stored_rows(service.repository)

    def forbidden(*args, **kwargs):
        raise AssertionError("Missing chart events must not read a saved case.")

    monkeypatch.setattr(ui, "workspace_snapshot", forbidden)
    callback_key, definition = next(
        (key, value)
        for key, value in app.callback_map.items()
        if ui.PREFIX + "drilldown.children" in key
    )
    inputs = dict(zip((ui.PREFIX + name for name in ui.FILTER_NAMES), values, strict=True))
    inputs.update(
        {
            "rv-session": state,
            "rv-page": page,
            ui.PREFIX + "lens": "topics",
            ui.PREFIX + "group_by": "channel",
            ui.PREFIX + "browse": 0,
        }
    )
    response = client.post(
        "/_dash-update-component",
        json={
            "output": callback_key,
            "outputs": [
                {"id": item.component_id, "property": item.component_property}
                for item in definition["output"]
            ],
            "inputs": [
                {"id": item["id"], "property": item["property"], "value": inputs.get(item["id"])}
                for item in definition["inputs"]
            ],
            "state": [],
            "changedPropIds": ["rv-page.value"],
        },
    )
    if page == "review":
        assert response.status_code == 204
    else:
        assert response.status_code == 200
        result = response.get_json()["response"]
        assert result[ui.PREFIX + "selected-record"] == {"options": [], "value": None}
        assert result[ui.PREFIX + "selection"]["data"] is None
        assert result[ui.PREFIX + "drilldown"]["children"].startswith("Select a time bar")
    assert stored_rows(service.repository) == before


def test_export_reopen_preserves_saved_annotations_without_local_execution(desk, tmp_path):
    original = desk.repository.get_bundle("saved-dashboard-case")
    before = stored_rows(desk.repository)
    package = export_case_package(original)
    reopened = read_case_package(
        package, tmp_path / "recipient-artifacts", install_artifacts=False
    ).bundle
    assert reopened.fingerprint() == original.fingerprint()
    projected = project_dashboard(reopened, filters={"sentiment_run_id": "saved-sentiment-run"})
    assert projected.sentiment_counts.model_dump() == {
        "positive": 1,
        "negative": 1,
        "neutral": 0,
        "mixed": 0,
        "unclear": 0,
        "unknown": 4,
    }
    for row in projected.rows:
        if row.sentiment == "unknown":
            assert row.sentiment_finding_id is None
            continue
        assert row.sentiment_target == "park plan"
        assert row.sentiment_fragment_ids == (row.id + ":quote",)
        finding = next(
            item for item in reopened.findings if item.snapshot_id == row.sentiment_finding_id
        )
        assert finding.details["annotation"]["model"] == "fixture-model@exact"
        assert finding.details["annotation"]["prompt_version"] == "fixture-prompt-v1"
        assert finding.run_id == "saved-sentiment-run"
    assert not reopened.reviews
    assert stored_rows(desk.repository) == before


def test_saved_projection_keeps_exact_filter_and_unknown_time_denominators(desk):
    bundle = desk.repository.get_bundle("saved-dashboard-case")
    before = stored_rows(desk.repository)
    projection = project_dashboard(
        bundle,
        excluded_record_ids=("r-other-platform",),
        filters={
            "text_query": "park",
            "platform": "telegram",
            "start": "2026-11-07",
            "end": "2026-11-07",
            "sentiment_run_id": "saved-sentiment-run",
        },
    )
    ids = {"r-positive", "r-negative", "r-date-only"}
    assert {row.id for row in projection.rows} == ids
    assert projection.coverage.excluded_records == 1
    assert projection.coverage.matching_records == 3
    assert projection.coverage.unknown_timestamp_records == 2
    assert projection.coverage.outside_date_records == 1
    assert len(projection.time_buckets) == 1
    assert projection.time_buckets[0].start == "2026-11-07"
    assert projection.time_buckets[0].record_ids == ("r-positive",)
    for group in (projection.platforms, projection.channels, projection.authors):
        assert sum(item.count for item in group) == len(ids)
        assert set().union(*(set(item.record_ids) for item in group)) == ids
    assert sum(projection.sentiment_counts.model_dump().values()) == len(ids)
    assert stored_rows(desk.repository) == before


def test_capture_reads_complete_saved_case_and_current_exclusions_without_mutation(desk):
    from evidence_explorer.social_dashboard import ui

    bundle = source_fixture()
    extra_text = "A later separately saved context record."
    extra = Record(
        id="later-record",
        kind="text",
        source_label="Later source context",
        text=extra_text,
        content_hash=hashlib.sha256(extra_text.encode()).hexdigest(),
    )
    bundle.records.append(extra)
    bundle.runs.append(
        Run(
            id="later-run",
            input_fingerprint="later",
            input_manifest=[extra.id],
            method="saved",
            method_version="1",
            created_at=NOW,
        )
    )
    desk.repository.import_bundle(bundle)
    scenario = desk.repository.create_scenario_state(bundle.case.id, "saved-sentiment-run")
    scenario = desk.repository.set_exclusion_state(
        scenario.id,
        "r-other-platform",
        True,
        "Synthetic scope test",
        scenario.version,
    )
    state = session_for_case(desk, bundle.case.id)
    state.update(scenario_id=scenario.id, scenario_version=scenario.version)
    before = stored_rows(desk.repository)
    projection, _, context = ui.capture_dashboard(desk, state)
    assert context
    assert {row["id"] for row in projection["rows"]} == {
        record.id for record in bundle.records if record.id != "r-other-platform"
    }
    assert projection["coverage"]["total_records"] == 7
    assert projection["coverage"]["excluded_records"] == 1
    assert projection["coverage"]["unknown_sentiment_records"] == 6
    assert stored_rows(desk.repository) == before


@pytest.mark.parametrize("field,value", [("case_fingerprint", "old"), ("scenario_version", 999)])
def test_capture_rejects_stale_case_or_scenario_context(desk, field, value):
    from evidence_explorer.social_dashboard import ui

    scenario = desk.repository.create_scenario_state("saved-dashboard-case", "saved-sentiment-run")
    state = session_for_case(desk, "saved-dashboard-case")
    state.update(scenario_id=scenario.id, scenario_version=scenario.version)
    state[field] = value
    before = stored_rows(desk.repository)
    with pytest.raises(ValueError):
        ui.capture_dashboard(desk, state)
    assert stored_rows(desk.repository) == before


def test_callbacks_render_saved_provenance_and_exact_bucket_drilldown_without_writes(
    dashboard, monkeypatch
):
    service, ui, app, callback = dashboard
    state = session_for_case(service, "saved-dashboard-case")
    values = filter_values(
        ui,
        text_query="park",
        platform="telegram",
        start="2026-11-07",
        end="2026-11-07",
        sentiment_run_id="saved-sentiment-run",
    )
    before = stored_rows(service.repository)
    output, capture, *_ = callback("output.children")(
        state, "compare", "topics", "channel", *values
    )
    assert capture is not None
    rendered = text(output)
    for expected in (
        "3 matching saved records",
        "Unknown timestamp: 2",
        "fixture-model@exact",
        "fixture-prompt-v1",
        "park plan",
        "saved synthetic sentiment annotation",
        "Denominator: 3 matching saved records",
        "1 have unknown sentiment",
    ):
        assert expected in rendered
    projection, _, _ = ui.capture_dashboard(service, state, capture["filters"])
    figure = ui.activity_figure(projection, capture["scope"])
    positive = next(trace for trace in figure.data if trace.name == "Positive")
    point = {"customdata": positive.customdata[0], "record_ids": ["r-outside"]}
    assert ui.derive_selection(projection, point, capture["scope"]) == ["r-positive"]
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id=ui.PREFIX + "activity-chart"))
    detail, choices, chosen, selection = callback("drilldown.children")(
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
    assert "Selected contribution · 1 saved records" in text(detail)
    codes = [node.children for node in nodes(detail) if isinstance(node, html.Code)]
    assert codes == ["r-positive"]
    assert [choice["value"] for choice in choices] == ["r-positive"]
    assert chosen is None and selection is not None
    open_exact = callback("selected-open.children")
    source_button = open_exact(
        "r-positive", selection, state, "compare", capture, "topics", "channel", *values
    )
    assert isinstance(source_button, html.Button)
    assert source_button.id["rid"] == "r-positive"
    assert source_button.id["context"] == capture["context"]
    refused = open_exact(
        "r-negative", selection, state, "compare", capture, "topics", "channel", *values
    )
    assert isinstance(refused, html.P)
    assert "does not contribute" in text(refused)
    assert all(not key.startswith("rv-session.") for key in app.callback_map)
    assert app.server.test_client().get("/_dash-layout").status_code == 200
    assert stored_rows(service.repository) == before


def test_account_chart_group_drilldown_uses_namespaced_current_record_ids(dashboard):
    service, ui, _, callback = dashboard
    state = session_for_case(service, "saved-dashboard-case")
    values = filter_values(ui, start="2026-11-07", end="2026-11-07")
    before = stored_rows(service.repository)
    output, capture, *_ = callback("output.children")(
        state, "compare", "accounts", "account", *values
    )
    assert capture is not None
    assert "Public account and channel presence" in text(output)
    projection, _, _ = ui.capture_dashboard(service, state, capture["filters"])
    figure = ui.activity_figure(projection, capture["scope"], "accounts", "account")
    group_points = [
        item for trace in figure.data for item in trace.customdata if "group_by" in item
    ]
    contributions = {
        tuple(ui.derive_selection(projection, {"customdata": item}, capture["scope"]))
        for item in group_points
    }
    assert ("r-positive",) in contributions
    assert ("r-other-platform",) in contributions
    assert ("r-positive", "r-other-platform") not in contributions
    assert stored_rows(service.repository) == before


@pytest.mark.parametrize("change", ["filters", "case", "scope", "record", "grouping"])
def test_drilldown_rejects_old_or_out_of_scope_browser_selections(dashboard, monkeypatch, change):
    service, ui, _, callback = dashboard
    state = session_for_case(service, "saved-dashboard-case")
    values = filter_values(ui, platform="telegram", start="2026-11-07", end="2026-11-07")
    _, capture, *_ = callback("output.children")(state, "compare", "topics", "channel", *values)
    assert capture is not None
    point = {"customdata": {"scope": capture["scope"], "kind": "record", "key": "r-positive"}}
    group_by = "channel"
    if change == "filters":
        values = filter_values(ui, platform="x", start="2026-11-07", end="2026-11-07")
    elif change == "case":
        state = {**state, "case_fingerprint": "old-fingerprint"}
    elif change == "scope":
        point["customdata"]["scope"] = "old-chart"
    elif change == "record":
        point["customdata"]["key"] = "r-outside"
    else:
        group_by = "account"
    before = stored_rows(service.repository)
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id=ui.PREFIX + "relationship-chart"))
    result, choices, chosen, selection = callback("drilldown.children")(
        None,
        {"points": [point]},
        0,
        capture,
        state,
        "compare",
        "topics",
        group_by,
        *values,
    )
    assert isinstance(result, html.P)
    assert choices == [] and chosen is None and selection is None
    assert "Selected contribution" not in text(result)
    assert not [node for node in nodes(result) if isinstance(node, html.Button)]
    assert stored_rows(service.repository) == before


def test_no_saved_sentiment_selection_stays_unknown_and_filters_clear_old_drilldown(
    dashboard, monkeypatch
):
    service, ui, app, callback = dashboard
    state = session_for_case(service, "saved-dashboard-case")
    values = filter_values(ui)
    before = stored_rows(service.repository)
    output, capture, *_ = callback("output.children")(
        state, "compare", "topics", "channel", *values
    )
    assert "Unknown sentiment: 6" in text(output)
    assert "No sentiment model is run" in text(output)
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id=ui.PREFIX + "capture"))
    cleared = callback("drilldown.children")(
        {"points": [{"customdata": {"scope": "old"}}]},
        None,
        0,
        capture,
        state,
        "compare",
        "topics",
        "channel",
        *values,
    )
    assert cleared[0].startswith("Select a time bar")
    assert cleared[1:] == ([], None, None)
    panel_nodes = list(nodes(app.layout))
    assert all(
        getattr(node, "storage_type", "memory") == "memory"
        for node in panel_nodes
        if isinstance(node, dcc.Store)
    )
    assert not any("queue" in str(getattr(node, "id", "")) for node in panel_nodes)
    assert stored_rows(service.repository) == before


def test_browse_all_opens_identical_undated_record_beyond_graph_limit_and_rejects_stale_filters(
    dashboard, monkeypatch
):
    service, ui, _, callback = dashboard
    content = "Identical saved wording with unknown date and source identity."
    digest = hashlib.sha256(content.encode()).hexdigest()
    records = [
        Record(
            id=f"undated-{index:03}",
            kind="text",
            source_label="Identical source label",
            text=content,
            content_hash=digest,
        )
        for index in range(81)
    ]
    case = EvidenceBundle(
        case=Case(id="undated-sample", title="Identical undated sample", question="Saved records"),
        records=records,
        fragments=[],
        runs=[
            Run(
                id="undated-run",
                input_fingerprint="undated-input",
                input_manifest=[record.id for record in records],
                method="saved text import",
                method_version="1",
                created_at=NOW,
            )
        ],
        findings=[],
    )
    service.repository.import_bundle(case)
    state = session_for_case(service, case.case.id)
    values = filter_values(ui)
    before = stored_rows(service.repository)
    _, capture, *_ = callback("output.children")(state, "compare", "topics", "channel", *values)
    projection, _, _ = ui.capture_dashboard(service, state, capture["filters"])
    assert projection["coverage"]["matching_records"] == 81
    assert projection["time_buckets"] == [] and projection["edges"] == []
    assert projection["options"]["authors"] == [] and projection["options"]["channels"] == []
    assert len(projection["rows"]) > ui.GRAPH_LIMIT > ui.TABLE_LIMIT

    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id=ui.PREFIX + "browse"))
    detail, choices, chosen, selection = callback("drilldown.children")(
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
    assert {choice["value"] for choice in choices} == {record.id for record in records}
    assert chosen is None and selection["point"]["customdata"]["kind"] == "matching"
    assert "81" in text(detail)
    source_button = callback("selected-open.children")(
        "undated-080",
        selection,
        state,
        "compare",
        capture,
        "topics",
        "channel",
        *values,
    )
    assert isinstance(source_button, html.Button)
    next_state = apply_navigation(service, state, source_button.id)
    assert next_state["record_id"] == "undated-080"
    assert next_state["case_id"] == case.case.id

    changed = filter_values(ui, text_query="wording that matches no saved record")
    refused = callback("selected-open.children")(
        "undated-080",
        selection,
        state,
        "compare",
        capture,
        "topics",
        "channel",
        *changed,
    )
    assert isinstance(refused, html.P)
    assert "changed" in text(refused).lower()
    rejected = callback("drilldown.children")(
        None,
        None,
        2,
        capture,
        state,
        "compare",
        "topics",
        "channel",
        *changed,
    )
    assert isinstance(rejected[0], html.P)
    assert rejected[1:] == ([], None, None)
    assert stored_rows(service.repository) == before


@pytest.mark.parametrize("page", ["review", "sources", "investigate", None])
def test_hidden_dashboard_callbacks_do_not_open_repository_or_capture_snapshot(
    dashboard, monkeypatch, page
):
    service, ui, _, callback = dashboard
    state = session_for_case(service, "saved-dashboard-case")
    values = filter_values(ui)
    _, capture, *_ = callback("output.children")(state, "compare", "topics", "channel", *values)
    selection = {
        "scope": capture["scope"],
        "point": {"customdata": {"kind": "matching", "scope": capture["scope"]}},
    }

    def forbidden(*args, **kwargs):
        raise AssertionError("A hidden dashboard must not read or capture the saved case.")

    monkeypatch.setattr(service.repository, "_connection", forbidden)
    monkeypatch.setattr(ui, "workspace_snapshot", forbidden)
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id=ui.PREFIX + "browse"))
    with pytest.raises(PreventUpdate):
        callback("output.children")(state, page, "topics", "channel", *values)
    with pytest.raises(PreventUpdate):
        callback("drilldown.children")(
            None, None, 1, capture, state, page, "topics", "channel", *values
        )
    with pytest.raises(PreventUpdate):
        callback("selected-open.children")(
            "r-positive", selection, state, page, capture, "topics", "channel", *values
        )
