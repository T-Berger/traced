"""Source selection previews remain read-only and cases retain their saved scope."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from dash import Dash, dcc, html, no_update
from dash.development.base_component import Component

from evidence_explorer.collection.models import CollectedBatch, CollectionSource
from evidence_explorer.config import Settings
from evidence_explorer.review.integrations import collection_scope_ui as ui
from evidence_explorer.review.integrations.sources import SourceItem
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService

CLOCK = datetime(2036, 1, 2, 10, tzinfo=UTC)


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


def workspace(path):
    settings = Settings(data_dir=path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    return WorkspaceService(repository, settings, path / "artifacts")


def add_source(service, label, *, published="2026-11-06T10:00:00Z", failed=False, empty=False):
    slug = label.lower().replace(" ", "-")
    source_id = service.add_collection_source(
        {
            "platform": "news",
            "label": label,
            "locator": f"https://publisher.example/{slug}/feed.xml",
            "enabled": False,
        }
    )
    if not empty:
        service.collect_source(source_id)
        job = service.collection.repository.claim()
        if failed:
            service.collection.repository.fail(job, "The public source could not be reached.")
        else:
            save_item(service, job, label, published)
    return source_id


def save_item(service, job, label, published, *, body=None):
    slug = label.lower().replace(" ", "-")
    saved = SourceItem(
        platform="news",
        channel_id=label,
        channel_name=label,
        native_id="post-1",
        text=body or f"The fictional {label} bulletin announced a public bridge inspection.",
        source_url=f"https://publisher.example/{slug}/post-1",
        published_at=published,
        acquired_at="2026-11-06T12:00:00Z",
    )
    service.collection.repository.complete(job, CollectedBatch(items=(saved,)))


def offline_source(service, key, *, platform="news", label=None, item=None, hour=0):
    """Seed a saved local observation without a provider, credential, or worker."""
    repository = service.collection.repository
    locator = {
        "news": f"https://publisher.example/{key}/feed.xml",
        "x": f"https://x.com/{key}",
        "reddit": f"https://www.reddit.com/r/{key}/",
    }.get(platform, f"https://publisher.example/{key}")
    source = CollectionSource(
        id=f"scope-{key}",
        platform=platform,
        label=label or key,
        locator=locator,
        enabled=False,
        content_mode="article_links",
    )
    now = CLOCK + timedelta(hours=hour)
    repository.add(source, now=now)
    if item is not None:
        repository.enqueue(source.id, now=now)
        job = repository.claim(now=now)
        repository.complete(job, CollectedBatch((item,)), now=now)
    return source.id


def observation(*, key="shared", platform="news", body="Saved fictional bulletin.", scope=None):
    methods = {
        "post_text": "x_api" if platform == "x" else "telegram_public",
        "title_description": "youtube_feed",
        "article_text": "rss_article",
        "feed_content": "rss_content",
        "feed_summary": "rss_content",
    }
    return SourceItem(
        platform=platform,
        channel_id="public-publisher",
        channel_name="Fictional public publisher",
        native_id=key,
        text=body,
        source_url=f"https://publisher.example/articles/{key}",
        acquired_at=CLOCK.isoformat(),
        provenance={"method": methods[scope], "content_scope": scope} if scope else None,
    )


def saved_renderer(service):
    app = Dash(__name__, suppress_callback_exceptions=True)
    app.layout = ui.saved_scope_panel
    ui.register_scope_callbacks(app, service)
    return next(
        item["callback"].__wrapped__
        for key, item in app.callback_map.items()
        if "live-case-saved-scope.children" in key
    )


@pytest.fixture
def desk(tmp_path):
    service = workspace(tmp_path / "original")
    ids = {
        "known": add_source(service, "Known bulletin"),
        "unknown": add_source(service, "Unknown date bulletin", published=None),
        "failed": add_source(service, "Unavailable source", failed=True),
        "unrelated": add_source(service, "Unrelated bulletin"),
        "empty": add_source(service, "Empty source", empty=True),
    }
    app = Dash(__name__, suppress_callback_exceptions=True)
    app.layout = lambda: html.Div(
        [
            dcc.Store(id="live-revision", data=0),
            dcc.Interval(id="live-poll", disabled=True),
            ui.scope_tools(),
            ui.saved_scope_panel(),
        ]
    )
    ui.register_scope_callbacks(app, service)

    def callback(output):
        return next(
            item["callback"].__wrapped__ for key, item in app.callback_map.items() if output in key
        )

    return service, app, callback, ids


def preview(callback, monkeypatch, selected):
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id="live-case-preview-action"))
    return callback("live-case-preview.children")(selected, 1)


def test_panel_requires_an_explicit_source_selection_and_uses_only_a_memory_token(desk):
    _, app, _, _ = desk
    nodes = list(components(app.layout()))
    selected = next(node for node in nodes if getattr(node, "id", None) == "live-case-sources")
    panel = next(node for node in nodes if getattr(node, "id", None) == "live-case-panel")
    create = next(node for node in nodes if getattr(node, "id", None) == "live-case-create")
    token = next(node for node in nodes if getattr(node, "id", None) == "live-case-token")
    assert selected.value == [] and selected.multi is True
    assert panel.open is False and create.disabled is True
    assert token.storage_type == "memory"
    assert "does not exclude evidence from an existing assessment" in text(panel)


def test_source_options_include_failed_and_empty_sources_without_changing_selection(desk):
    service, app, callback, ids = desk
    choices = callback("live-case-sources.options")(0, 0)
    assert {choice["value"] for choice in choices} == set(ids.values())
    failed = next(choice["label"] for choice in choices if choice["value"] == ids["failed"])
    assert "Unavailable source" in failed and "0 saved" in failed and "Collection failed" in failed
    added = add_source(service, "Another empty source", empty=True)
    updated = callback("live-case-sources.options")(1, 1)
    assert {choice["value"] for choice in updated} == {*ids.values(), added}
    assert not any("live-case-sources.value" in key for key in app.callback_map)
    assert service.list_cases() == []


def test_preview_shows_selected_failed_scope_and_unknown_times_without_saving(desk, monkeypatch):
    service, _, callback, ids = desk
    before = service.collection_status()["jobs"]
    panel, token, disabled, notice = preview(callback, monkeypatch, [ids["unknown"], ids["failed"]])
    rendered = text(panel)
    assert "Unknown date bulletin" in rendered and "Unavailable source" in rendered
    assert "Unrelated bulletin" not in rendered and "Known bulletin" not in rendered
    assert "Collection failed" in rendered and "could not be reached" in rendered
    assert "1 with unknown publication time" in rendered
    assert disabled is False and "Preview only" in text(notice)
    assert set(token) == {"fingerprint", "source_ids"}
    assert token["source_ids"] == sorted([ids["unknown"], ids["failed"]])
    assert service.list_cases() == [] and service.collection_status()["jobs"] == before


def test_changing_selection_invalidates_preview_and_old_token_cannot_create(desk, monkeypatch):
    service, _, callback, ids = desk
    _, token, _, _ = preview(callback, monkeypatch, [ids["known"]])
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id="live-case-sources"))
    panel, cleared, disabled, message = callback("live-case-preview.children")([ids["unknown"]], 1)
    assert panel == "" and cleared is None and disabled is True
    assert "Preview this selection" in text(message)
    result = callback("rv-import-success.children")(1, token, [ids["unknown"]])
    assert all(value is no_update for value in result[:5])
    assert "Preview this source selection again" in text(result[-1])
    assert service.list_cases() == []


def test_empty_and_all_failed_selections_cannot_create_cases(desk, monkeypatch):
    service, _, callback, ids = desk
    panel, token, disabled, message = preview(callback, monkeypatch, [])
    assert panel == "" and token is None and disabled is True
    assert "Choose at least one" in text(message)
    panel, token, disabled, message = preview(callback, monkeypatch, [ids["failed"], ids["empty"]])
    assert "Unavailable source" in text(panel) and "Empty source" in text(panel)
    assert disabled is True and "No saved records" in text(message)
    result = callback("rv-import-success.children")(1, token, token["source_ids"])
    assert all(value is no_update for value in result[:5])
    assert "No saved records" in text(result[-1])
    assert service.list_cases() == []


def test_repeated_source_ids_are_rejected_without_saving_or_collecting(desk, monkeypatch):
    service, _, callback, ids = desk
    before = service.collection_status()["jobs"]
    panel, token, disabled, message = preview(callback, monkeypatch, [ids["known"], ids["known"]])
    assert panel == "" and token is None and disabled is True
    assert "only once" in text(message)
    assert service.collection_status()["jobs"] == before and service.list_cases() == []


def test_creation_opens_only_selected_evidence_and_keeps_failed_source_in_scope(desk, monkeypatch):
    service, _, callback, ids = desk
    selected = [ids["failed"], ids["unknown"]]
    _, token, _, _ = preview(callback, monkeypatch, selected)
    options, case_id, state, page, message, error = callback("rv-import-success.children")(
        1, token, selected
    )
    assert options == [{"label": service.load_case(case_id).case.title, "value": case_id}]
    assert state["case_id"] == case_id and state["finding_id"] and state["record_id"]
    assert state["scenario_id"] is None and page == "review" and error == ""
    assert "selected sources" in text(message)
    bundle = service.load_case(case_id)
    assert (
        len(bundle.records) == 1
        and bundle.records[0].metadata["channel_name"] == "Unknown date bulletin"
    )
    assert bundle.records[0].published_at is None
    scope = bundle.case.coverage["collection_scope"]
    assert scope["source_ids"] == sorted(selected)
    assert {source["label"] for source in scope["sources"]} == {
        "Unknown date bulletin",
        "Unavailable source",
    }
    assert not bundle.scenarios and not bundle.reviews
    assert callback("rv-import-success.children")(2, token, list(reversed(selected)))[1] == case_id
    assert len(service.list_cases()) == 1


def test_saved_refresh_after_preview_requires_another_preview_without_changing_existing_case(
    desk, monkeypatch
):
    service, _, callback, ids = desk
    selected = [ids["known"]]
    _, token, _, _ = preview(callback, monkeypatch, selected)
    original_id = callback("rv-import-success.children")(1, token, selected)[1]
    original = service.load_case(original_id).model_dump()
    service.collect_source(ids["known"])
    job = service.collection.repository.claim()
    save_item(
        service, job, "Known bulletin", None, body="A revised fictional bridge inspection notice."
    )
    rejected = callback("rv-import-success.children")(2, token, selected)
    assert all(value is no_update for value in rejected[:5])
    assert "changed" in text(rejected[-1]).lower() and "preview" in text(rejected[-1]).lower()
    assert (
        len(service.list_cases()) == 1 and service.load_case(original_id).model_dump() == original
    )


def test_reopened_case_renders_its_saved_scope_without_reading_current_sources(
    desk, monkeypatch, tmp_path
):
    service, _, callback, ids = desk
    selected = [ids["unknown"], ids["failed"]]
    _, token, _, _ = preview(callback, monkeypatch, selected)
    case_id = callback("rv-import-success.children")(1, token, selected)[1]
    restored = workspace(tmp_path / "restored")
    assert restored.import_package(service.export_package(case_id)) == case_id
    monkeypatch.setattr(
        restored, "list_collection_sources", lambda: pytest.fail("Live source read")
    )
    monkeypatch.setattr(ui, "session_for_case", lambda *_: pytest.fail("Read must not navigate"))
    restored_app = Dash(__name__, suppress_callback_exceptions=True)
    restored_app.layout = ui.saved_scope_panel
    ui.register_scope_callbacks(restored_app, restored)
    render = next(
        item["callback"].__wrapped__
        for key, item in restored_app.callback_map.items()
        if "live-case-saved-scope.children" in key
    )
    panel = render({"case_id": case_id})
    assert "Saved source scope" in text(panel)
    assert "Unavailable source: Collection failed" in text(panel)
    assert "could not be reached" in text(panel)
    assert "1 with unknown publication time" in text(panel)
    assert "saved with the case" in text(panel)
    assert restored.list_cases()[0]["id"] == case_id


def test_unexpected_preview_failure_is_generic_and_exposes_no_source_payload(desk, monkeypatch):
    _, _, callback, ids = desk
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id="live-case-preview-action"))
    service = desk[0]

    def fail(_ids):
        raise RuntimeError("private-token-and-source-payload")

    monkeypatch.setattr(service, "preview_collection_case", fail)
    panel, token, disabled, message = callback("live-case-preview.children")([ids["known"]], 1)
    assert panel == "" and token is None and disabled is True
    assert "could not be previewed" in text(message)
    assert "private-token" not in text(message)


@pytest.mark.parametrize(
    ("scope", "label"),
    [
        ("post_text", "Post text: 1"),
        ("title_description", "Video titles and descriptions (no transcript): 1"),
        ("article_text", "Article text: 1"),
        ("feed_content", "Feed-supplied content: 1"),
        ("feed_summary", "Feed summaries (not full articles): 1"),
        (None, "Unknown saved content type: 1"),
    ],
)
def test_content_labels_describe_included_saved_provenance_not_source_configuration(
    tmp_path, scope, label
):
    service = workspace(tmp_path / "content")
    source_id = offline_source(service, "content", item=observation(scope=scope))
    summary = service.preview_collection_case([source_id])
    rendered = text(ui.scope_preview(summary))
    assert label in rendered
    assert "Full article text" not in rendered
    if scope != "article_text":
        assert "Article text: 1" not in rendered
    if scope is None:
        assert "Feed summaries (not full articles): 1" not in rendered
    assert summary["sources"][0]["content_scopes"] == {scope or "unknown": 1}


def test_setup_readiness_does_not_replace_collection_history(tmp_path, monkeypatch):
    service = workspace(tmp_path / "access")
    x_id = offline_source(
        service,
        "publicaccount",
        platform="x",
        label="Saved X account",
        item=observation(platform="x", scope="post_text"),
    )
    reddit_id = offline_source(service, "publicforum", platform="reddit", label="Reddit forum")
    monkeypatch.setattr(
        service.collection,
        "readiness",
        lambda platform: {"ready": False, "message": f"{platform} developer setup is missing."},
    )
    panel = ui.scope_preview(service.preview_collection_case([x_id, reddit_id]))
    rows = [node for node in components(panel) if isinstance(node, html.Tr)]
    x = next(row for row in rows if "Saved X account" in text(row))
    reddit = next(row for row in rows if "Reddit forum" in text(row))
    assert "Setup or access needed" in text(x.children[3])
    assert "x developer setup is missing" in text(x.children[3])
    assert "Collected" in text(x.children[4])
    assert "developer setup" not in text(x.children[4])
    assert "reddit developer setup is missing" in text(reddit.children[3])
    assert "Not collected yet" in text(reddit.children[4])


def test_reopened_scope_preserves_content_access_and_omissions_without_current_source_reads(
    tmp_path, monkeypatch
):
    service = workspace(tmp_path / "metadata")
    old_id = offline_source(
        service,
        "older",
        label="Older selected feed",
        item=observation(body="Omitted earlier fictional text.", scope="article_text"),
    )
    preferred_id = offline_source(
        service,
        "preferred",
        label="Preferred selected feed",
        item=observation(body="Included fictional summary.", scope="feed_summary"),
        hour=1,
    )
    legacy_id = offline_source(
        service, "legacy", label="Legacy source", item=observation(key="legacy")
    )
    repository = service.collection.repository
    with repository.connection(write=True) as connection:
        connection.execute(
            "UPDATE collection_source_items SET payload=NULL,semantic_hash=NULL,"
            "first_seen_at=NULL,last_seen_at=NULL WHERE source_id=?",
            (legacy_id,),
        )
    x_id = offline_source(service, "credentialgap", platform="x", label="X setup gap")
    original_readiness = service.collection.readiness
    monkeypatch.setattr(
        service.collection,
        "readiness",
        lambda platform: (
            {"ready": False, "message": "X developer credentials are missing."}
            if platform == "x"
            else original_readiness(platform)
        ),
    )
    selected = [old_id, preferred_id, legacy_id, x_id]
    summary = service.preview_collection_case(selected)
    case_id = service.open_collection_case(selected, expected_fingerprint=summary["fingerprint"])
    restored = workspace(tmp_path / "reopened-metadata")
    assert restored.import_package(service.export_package(case_id)) == case_id
    assert restored.load_case(case_id).case.coverage["collection_scope"] == summary
    monkeypatch.setattr(
        restored.collection, "readiness", lambda *_: pytest.fail("Current readiness read")
    )
    monkeypatch.setattr(
        restored, "list_collection_sources", lambda: pytest.fail("Current source read")
    )
    panel = saved_renderer(restored)({"case_id": case_id})
    rendered = text(panel)
    assert "Feed summaries (not full articles): 1" in rendered
    assert "Article text: 1" not in rendered
    assert "X developer credentials are missing" in rendered
    assert "Unknown source attribution: 1 saved record link(s) omitted" in rendered
    assert "Older selected feed" in rendered and "1 differing saved version(s) omitted" in rendered
    assert "Preferred selected observation" in rendered and "Preferred selected feed" in rendered
    assert "Native item: public-publisher / shared" in rendered
    omitted = next(row for row in summary["sources"] if row["id"] == old_id)["version_omissions"][0]
    for field in (
        "observed_at",
        "payload_hash",
        "content_hash",
        "included_observed_at",
        "included_payload_hash",
        "included_content_hash",
    ):
        assert omitted[field] in rendered
    assert "Omitted earlier fictional text" not in rendered
    assert "not included or available in this case or its package" in rendered
    visible_summary = text(
        [child for child in panel.children if not isinstance(child, html.Details)]
    )
    assert "One newest saved version per native item from selected sources" in visible_summary
    assert "source-ID tie-break is a selection convention" in visible_summary
    assert "Legacy source" in visible_summary and "X setup gap" in visible_summary
    assert len(restored.load_case(case_id).records) == 1


def test_omitted_metadata_and_case_limit_are_explained_without_claiming_text_edits():
    omission = {
        "channel_id": "public-publisher",
        "native_id": "native-42",
        "observed_at": "2036-01-02T10:00:00+00:00",
        "payload_hash": "a" * 64,
        "content_hash": "c" * 64,
        "included_source_id": "preferred",
        "included_observed_at": "2036-01-02T10:00:00+00:00",
        "included_payload_hash": "b" * 64,
        "included_content_hash": "c" * 64,
        "included_in_case": False,
        "observation_time_tied": True,
        "reason": "Equal observation times use the source-ID selection convention.",
    }
    panel = ui.scope_preview(
        {
            "sources": [
                {
                    "id": "omitted",
                    "label": "Omitted feed",
                    "omitted_version_count": 1,
                    "version_omissions": [omission],
                },
                {"id": "preferred", "label": "Preferred feed"},
            ],
            "version_policy": "latest_selected_observation_then_source_id",
            "truncated": True,
        }
    )
    rendered = text(panel)
    assert "Preferred feed" in rendered and "Preferred selected observation" in rendered
    assert "Outside case record limit" in rendered and "Included in this case" not in rendered
    assert "Full saved observation payload hash" in rendered
    assert "Saved text content hash" in rendered
    assert "Saved text content hashes match; other observation metadata differs" in rendered
    assert "This does not prove which platform edit is newest" in rendered
    assert "Hashes identify saved observations; they are not readable evidence" in rendered
    assert omission["payload_hash"] in rendered and omission["included_payload_hash"] in rendered
    region = next(node for node in components(panel) if getattr(node, "role", None) == "region")
    assert not any(isinstance(node, html.Details) for node in components(region))
    assert "1 differing saved version(s) omitted" in text(region)
    assert omission["payload_hash"] not in text(region)
    disclosure = next(node for node in components(panel) if isinstance(node, html.Details))
    assert isinstance(disclosure.children[0], html.Summary)
    assert text(disclosure.children[0]).startswith("Omitted feed: inspect 1")
    assert disclosure.style == {
        "minWidth": 0,
        "maxWidth": "100%",
        "overflowWrap": "anywhere",
    }
    for node in components(disclosure):
        if isinstance(node, (html.Li, html.Dl, html.Dd)):
            assert node.style == disclosure.style


def test_legacy_saved_scope_leaves_unrecorded_metadata_explicitly_unknown():
    scope = {
        "sources": [
            {
                "id": "legacy-video",
                "label": "Legacy video source",
                "platform": "youtube",
                "included_count": 1,
                "item_count": 1,
                "content_mode": "article_links",
            }
        ],
        "record_count": 1,
    }
    service = SimpleNamespace(
        get_case=lambda _id: {"case": {"coverage": {"collection_scope": scope}}}
    )
    rendered = text(saved_renderer(service)({"case_id": "saved-legacy"}))
    assert "Content type was not recorded" in rendered
    assert "Readiness was not recorded" in rendered
    assert "Attribution and version omissions were not recorded" in rendered
    assert "Saved-version selection policy was not recorded" in rendered
    assert "Video titles and descriptions" not in rendered and "Article text: 1" not in rendered


def test_source_table_has_a_keyboard_scroll_region_and_semantic_source_headers(desk):
    service, _, _, ids = desk
    panel = ui.scope_preview(service.preview_collection_case([ids["known"], ids["empty"]]))
    nodes = list(components(panel))
    region = next(node for node in nodes if getattr(node, "role", None) == "region")
    assert region.tabIndex == 0 and "traced-focus" in region.className.split()
    assert getattr(region, "aria-label") == "Selected sources and saved collection coverage"
    assert region.style == {"overflowX": "auto", "minWidth": 0, "maxWidth": "100%"}
    assert panel.style["minWidth"] == 0 and panel.style["maxWidth"] == "100%"
    table = next(node for node in components(region) if isinstance(node, html.Table))
    assert any(isinstance(node, html.Caption) for node in components(table))
    headings = [node for node in components(table) if isinstance(node, html.Th)]
    assert len([node for node in headings if node.scope == "col"]) == 6
    assert len([node for node in headings if node.scope == "row"]) == 2
    assert "Scroll the table sideways" in text(panel)
