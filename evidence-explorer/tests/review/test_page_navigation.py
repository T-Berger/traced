"""Selected tabs control mounted pages without queued visibility callbacks."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from importlib.resources import files

import pytest
from dash import html, no_update
from dash.development.base_component import Component

from evidence_explorer.config import Settings
from evidence_explorer.review.components import page_navigation
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.integrations.web import create_app as integrated_app
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.service import ReviewService
from evidence_explorer.review.storage import ReviewRepository
from evidence_explorer.review.web import create_app

PAGES = ("cases", "review", "compare", "brief")
PAGE_IDS = {f"rv-{page}-page" for page in PAGES}


def components(node):
    if isinstance(node, Component):
        yield node
        yield from components(getattr(node, "children", None))
    elif isinstance(node, (list, tuple)):
        for child in node:
            yield from components(child)


def make_desk(tmp_path, variant, *, populated=True):
    if variant == "integrated":
        settings = Settings(data_dir=tmp_path)
        repository = WorkspaceRepository(settings.db_path)
        repository.initialize()
        service = WorkspaceService(repository, settings, tmp_path / "artifacts")
        if populated:
            service.import_bundle(load_demo_bundle().model_dump(mode="json"))
        app = integrated_app(service)
        prefix = "/"
    else:
        repository = ReviewRepository(tmp_path / "review.sqlite3")
        repository.initialize()
        service = ReviewService(repository)
        if populated:
            service.seed_demo()
        app = create_app(service, prefix="/review/")
        prefix = "/review/"
    return service, app, prefix


@pytest.fixture(params=["standalone", "integrated"])
def desk(tmp_path, request):
    return make_desk(tmp_path, request.param)


@pytest.mark.parametrize("variant", ["standalone", "integrated"])
@pytest.mark.parametrize("populated", [False, True])
def test_initial_native_tab_matches_empty_or_populated_workspace(tmp_path, variant, populated):
    service, app, _ = make_desk(tmp_path, variant, populated=populated)
    before = service.list_cases()
    navigation = next(
        node for node in components(app.layout()) if getattr(node, "id", None) == "rv-page"
    )
    assert navigation.value == ("review" if populated else "cases")
    assert bool(before) is populated
    assert service.list_cases() == before


@pytest.mark.parametrize("selected", PAGES)
def test_each_native_tab_has_an_unambiguous_selected_page_class(selected):
    navigation = page_navigation(selected)
    assert navigation.id == "rv-page" and navigation.value == selected
    assert [tab.value for tab in navigation.children] == list(PAGES)
    for tab in navigation.children:
        assert set(tab.selected_className.split()) == {
            "rv-navigation-tab--selected",
            f"rv-navigation-tab--selected-{tab.value}",
        }
        assert "rv-navigation-tab--selected" not in tab.className.split()


def test_pages_stay_mounted_and_visibility_has_no_callback_writer(desk):
    service, app, prefix = desk
    cases_before = service.list_cases()
    layout = app.layout()
    main = next(
        node for node in components(layout) if getattr(node, "className", None) == "rv-main"
    )
    pages = [node for node in main.children if getattr(node, "id", None) in PAGE_IDS]
    assert len(pages) == 4
    assert {page.id for page in pages} == PAGE_IDS
    assert all(isinstance(page, html.Section) for page in pages)
    page_writers = []
    for key, definition in app.callback_map.items():
        outputs = definition["output"]
        for output in outputs if isinstance(outputs, list) else [outputs]:
            assert not (output.component_id in PAGE_IDS and output.component_property == "style")
            if output.component_id == "rv-page" and output.component_property == "value":
                inputs = tuple((item["id"], item["property"]) for item in definition["inputs"])
                page_writers.append((inputs, output.allow_duplicate))
                assert ("rv-page.value@" in key) is output.allow_duplicate
    expected_writers = {
        ((("rv-session", "data"),), False),
        ((("rv-import-commit", "n_clicks"),), True),
    }
    if prefix == "/":
        expected_writers.update(
            {
                ((("trail-source-commit", "n_clicks"),), True),
                ((("live-case-create", "n_clicks"),), True),
            }
        )
    assert len(page_writers) == len(expected_writers)
    assert set(page_writers) == expected_writers
    assert service.list_cases() == cases_before


def test_served_css_maps_selected_class_to_exact_page_without_async_state(desk):
    _, app, prefix = desk
    client = app.server.test_client()
    index = client.get(prefix).get_data(as_text=True)
    assert prefix + "assets/page_navigation.css" in index
    response = client.get(prefix + "assets/page_navigation.css")
    expected = files("evidence_explorer.review").joinpath("assets/page_navigation.css").read_text()
    assert response.status_code == 200 and response.get_data(as_text=True) == expected
    css = re.sub(r"/\*.*?\*/", "", expected, flags=re.S)
    rules = {
        " ".join(selector.split()): " ".join(body.split())
        for selector, body in re.findall(r"([^{}]+)\{([^{}]+)\}", css)
    }
    assert len(rules) == 2
    default = (
        ".review-app > .rv-main > :is(" + ", ".join(f"#rv-{page}-page" for page in PAGES) + ")"
    )
    assert rules.pop(default) == "display: none !important;"
    selected_selectors, selected_declaration = rules.popitem()
    assert selected_declaration == "display: block !important;"
    selectors = {selector.strip() for selector in selected_selectors.split(",")}
    for tab in page_navigation().children:
        selected_class = f"rv-navigation-tab--selected-{tab.value}"
        selector = f".review-app:has(#rv-page .{selected_class}) > .rv-main > #rv-{tab.value}-page"
        selectors.remove(selector)
    assert not selectors


def test_navigation_keeps_existing_session_destination_and_focus_inputs(desk):
    _, app, _ = desk
    navigate = app.callback_map["rv-page.value"]["callback"].__wrapped__
    assert navigate({"navigate_to": "brief"}) == "brief"
    assert navigate({}) is no_update
    focus = app.callback_map["rv-scroll-marker.data"]
    assert focus["inputs"] == [
        {"id": "rv-page", "property": "value"},
        {"id": "rv-session", "property": "data"},
    ]


@pytest.mark.parametrize("mode", ["exact_target", "same_page_target", "tab_only", "unknown_target"])
def test_deferred_focus_preserves_target_behavior_without_awaiting_page_styles(desk, mode):
    _, app, _ = desk
    registration = next(
        item for item in app._callback_list if item["output"] == "rv-scroll-marker.data"
    )
    node = shutil.which("node")
    assert node, "Node is required to check the actual registered focus function"
    script = r"""
const vm = require('vm'), fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const events = [], frames = [], parent = {tagName: 'DETAILS', open: false};
const target = {
    tagName: 'DIV', parentElement: parent, getClientRects: () => [{}],
    matches: () => false, setAttribute: (name, value) => events.push(['attribute', name, value]),
    focus: options => events.push(['focus', options]),
    scrollIntoView: options => events.push(['target-scroll', options])
};
const window = {
    dash_clientside: {}, scrollTo: (...args) => events.push(['window-scroll', ...args])
};
const runtime = vm.createContext({
    window, dash_clientside: window.dash_clientside,
    document: {getElementById: id => id === 'rv-review-reason' ? target : null},
    getComputedStyle: () => ({display: 'block'}),
    requestAnimationFrame: fn => frames.push(fn)
});
input.scripts.forEach(script => vm.runInContext(script, runtime));
const definition = input.definition;
const fn = window.dash_clientside[definition.namespace][definition.function_name];
const state = {case_id: 'saved-case'};
if (input.mode !== 'tab_only') {
    state.navigate_to = 'review';
    state.navigate_target = input.mode === 'unknown_target' ? 'unknown-target' : 'rv-review-reason';
}
window.dash_clientside.callback_context = {triggered: [{
    prop_id: input.mode === 'same_page_target' ? 'rv-session.data' : 'rv-page.value'
}]};
const result = fn('review', state);
if (result && typeof result.then === 'function') throw Error('Focus must not hold a Dash promise');
const eventsBeforeFrames = [...events];
while (frames.length) frames.shift()();
process.stdout.write(JSON.stringify({result, events, eventsBeforeFrames, parentOpen: parent.open}));
"""
    completed = subprocess.run(
        [node, "-e", script],
        input=json.dumps(
            {
                "scripts": app._inline_scripts,
                "definition": registration["clientside_function"],
                "mode": mode,
            }
        ),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    observed = json.loads(completed.stdout)
    assert observed["result"] == {"page": "review", "case_id": "saved-case"}
    assert observed["eventsBeforeFrames"] == []
    if mode in {"exact_target", "same_page_target"}:
        assert observed["parentOpen"] is True
        assert observed["events"] == [
            ["attribute", "tabindex", "-1"],
            ["focus", {"preventScroll": True}],
            ["target-scroll", {"block": "start", "behavior": "auto"}],
        ]
    else:
        assert observed["events"] == [["window-scroll", 0, 0]]
