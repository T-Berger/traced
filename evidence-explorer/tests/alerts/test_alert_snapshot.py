import json
import re
import zlib
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from evidence_explorer.alerts import analysis
from evidence_explorer.alerts.evidence import evidence_item
from evidence_explorer.alerts.repository import AnalyticsRepository, CaseData
from evidence_explorer.alerts.settings import AlertSettings
from evidence_explorer.alerts.ui import _snapshot_fingerprint


def _digest(text):
    return sha256(text.encode()).hexdigest()


def _post():
    raw = '{"id":"one"}'
    return {
        "id": "one",
        "case_id": "case-a",
        "status": "classified",
        "attribution": "third_party",
        "platform": "x",
        "disinformation": 0.95,
        "disinfo_verdict": "contradicted_by_subject",
        "reference_ids": ["ref"],
        "url": "https://example.org/one",
        "platform_item_id": "one",
        "handle": "someone",
        "platform_user_id": "1",
        "collected_at": datetime.now(UTC),
        "capture_sha256": _digest(raw),
        "text": "extracted wording",
        "author_id": "author",
    }, raw


def _reference(case="case-a"):
    raw = '{"id":"ref"}'
    return {
        "id": "ref",
        "case_id": case,
        "text": "context",
        "raw": raw,
        "capture_sha256": _digest(raw),
    }


def test_missing_or_cross_case_context_is_never_treated_as_available():
    row, _ = _post()
    settings = AlertSettings.defaults()
    for refs in ({}, {"ref": _reference("another-case")}):
        a = analysis.assess(row, settings, refs)
        assert a.status == analysis.NEEDS
        assert not next(c for c in a.checks if c.key == "proof").ok
        assert "decision" in analysis.verdict([a])["detail"]
    a = analysis.assess(row, settings, {"ref": _reference()})
    assert a.status == analysis.READY
    assert analysis.STATUS_LABELS[a.status] == "Candidate for review"
    assert "does not establish falsity" in next(c for c in a.checks if c.key == "proof").detail


def test_evidence_item_requires_exact_selected_and_reference_captures():
    row, raw = _post()
    refs = {"ref": _reference()}
    a = analysis.assess(row, AlertSettings.defaults(), refs)
    for missing in (None, '{"changed":true}'):
        with pytest.raises(ValueError, match="capture|Capture"):
            evidence_item(a, refs, missing)
    with pytest.raises(ValueError, match="selected case"):
        evidence_item(a, {"ref": _reference("other")}, raw)
    ref = _reference()
    ref["raw"] = "changed"
    with pytest.raises(ValueError, match="changed"):
        evidence_item(a, {"ref": ref}, raw)
    item = evidence_item(a, refs, raw)
    assert item["preservation"]["capture_sha256"] == _digest(raw)
    assert item["context"]["subject_posts"][0]["capture_sha256"] == refs["ref"]["capture_sha256"]


def test_fingerprint_detects_changed_displayed_data_without_changed_raw():
    row, _ = _post()
    data = CaseData({"id": "case-a"}, 1, [row], {"ref": _reference()}, None)
    settings = AlertSettings.defaults()
    before = _snapshot_fingerprint(data, settings)
    row["text"] = "changed extracted text"
    assert before != _snapshot_fingerprint(data, settings)
    assert (
        "no human"
        in analysis.verdict([analysis.assess(row, settings, data.references)])["detail"]
    )


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class _Connection:
    def __init__(self):
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query, args=None):
        self.queries.append((query, args))
        if query.startswith("SET TRANSACTION"):
            return _Result([])
        if "FROM cases WHERE" in query:
            return _Result([{"id": "case-a"}])
        if "SELECT count(*) AS n FROM content_items" in query:
            return _Result([{"n": 1}])
        if "FROM analytics_content_labels l" in query:
            return _Result([{"id": "one", "case_id": "case-a", "reference_ids": ["ref"]}])
        if "SELECT id::text AS id, case_id::text AS case_id" in query:
            return _Result([_reference()])
        if "to_regclass" in query:
            return _Result([{"ok": False}])
        if "SELECT c.id::text AS id, c.raw::text AS raw" in query:
            return _Result([{"id": "one", "raw": '{"id":"one"}', "run_id": None}])
        raise AssertionError(query)


def test_snapshot_scopes_labels_references_and_captures_in_one_repeatable_read():
    conn = _Connection()
    repo = AnalyticsRepository()
    repo._connection = lambda: conn
    data = repo.case_snapshot("case-a", ["one"])
    assert conn.queries[0][0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    labels = next(q for q in conn.queries if "FROM analytics_content_labels l" in q[0])
    references = next(
        q for q in conn.queries if "SELECT id::text AS id, case_id::text AS case_id" in q[0]
    )
    captures = next(
        q for q in conn.queries if "SELECT c.id::text AS id, c.raw::text AS raw" in q[0]
    )
    assert "c.case_id = l.case_id" in labels[0]
    assert "case_id::text = %s" in references[0] and references[1][0] == "case-a"
    assert "c.case_id::text = %s" in captures[0] and captures[1][1] == "case-a"
    assert data.provenance["one"]["raw"] == '{"id":"one"}'


def test_report_requires_case_scope_and_attaches_selected_and_context_records():
    from evidence_explorer.alerts.pdf_report import build_pdf

    row, raw = _post()
    refs = {"ref": _reference()}
    settings = AlertSettings.defaults()
    a = analysis.assess(row, settings, refs)
    provenance = {"one": {"raw": raw, "run": None}}
    with pytest.raises(ValueError, match="selected case"):
        build_pdf({"id": "other"}, [a], refs, provenance, [], settings)
    with pytest.raises(ValueError, match="Saved captures"):
        build_pdf({"id": "case-a"}, [a], refs, {}, [], settings)
    content, filename, digest = build_pdf(
        {"id": "case-a", "slug": "case-a", "subject_name": "Example"},
        [a],
        refs,
        provenance,
        [],
        settings,
    )
    assert content.startswith(b"%PDF")
    assert b"context-ref.json" in content
    assert b"one.json" in content
    streams = []
    for stream in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", content, re.DOTALL):
        try:
            streams.append(zlib.decompress(stream))
        except zlib.error:
            streams.append(stream)
    assert raw.encode() in streams
    assert refs["ref"]["raw"].encode() in streams
    manifest = next(json.loads(x) for x in streams if x.startswith(b'{\n  "kind":'))
    item = manifest["items"][0]
    assert manifest["case"]["id"] == "case-a"
    assert manifest["screening_settings"]["categories"]["disinformation"]["filing_score"] == 0.8
    assert item["classification"]["score"] == row["disinformation"]
    assert item["classification"]["classified_at"] is None
    assert item["classification"]["run_id"] is None
    assert item["preservation"]["capture_file"] == "one.json"
    assert item["preservation"]["capture_sha256"] == _digest(raw)
    assert item["context"]["subject_posts"][0]["capture_file"] == "context-ref.json"
    assert item["context"]["subject_posts"][0]["capture_sha256"] == _digest(refs["ref"]["raw"])
    assert digest == sha256(content).hexdigest()
    assert filename.endswith(".pdf")


def test_archive_link_is_not_treated_as_preserved_archive_copy():
    row, _ = _post()
    row["archive_url"] = "https://example.org/archive"
    settings = AlertSettings.defaults()
    settings.require_archive_copy = True
    a = analysis.assess(row, settings, {"ref": _reference()})
    assert a.status == analysis.NEEDS
    assert not next(c for c in a.checks if c.key == "archive").ok


def test_download_callback_rechecks_exact_snapshot_selection_and_captures(monkeypatch, tmp_path):
    from dash import no_update

    from evidence_explorer.alerts import ui
    from evidence_explorer.config import Settings
    from evidence_explorer.review.integrations import web
    from evidence_explorer.review.integrations.workspace import (
        WorkspaceRepository,
        WorkspaceService,
    )

    row, raw = _post()
    case_data = CaseData(
        {"id": "case-a", "slug": "case-a", "subject_name": "Example"},
        1,
        [row],
        {"ref": _reference()},
        None,
        provenance={"one": {"raw": raw, "run": None}},
    )

    class FakeRepository:
        reads = 0

        def case_data(self, case_id, refresh=False):
            type(self).reads += 1
            return case_data

        def case_snapshot(self, case_id, item_ids):
            assert case_id == "case-a"
            assert item_ids
            return case_data

    monkeypatch.setattr(ui, "AnalyticsRepository", FakeRepository)
    settings = Settings(data_dir=tmp_path / "workspace")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    app = web.create_app(WorkspaceService(repository, settings, settings.data_dir / "artifacts"))
    render = next(
        entry["callback"].__wrapped__
        for key, entry in app.callback_map.items()
        if "al-selection-snapshot.data" in key
    )
    assert render("case-a", None, 0, "cases", []) == (no_update,) * 8
    assert FakeRepository.reads == 0
    from types import SimpleNamespace

    from dash.development.base_component import Component

    def strings(node):
        if isinstance(node, str):
            yield node
        elif isinstance(node, (list, tuple)):
            for child in node:
                yield from strings(child)
        elif isinstance(node, Component):
            yield from strings(getattr(node, "children", None))

    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id="al-case"))
    row["disinformation"] = 0.7
    rendered = render("case-a", None, 0, "alerts", [])
    assert "Below review threshold" in list(strings(rendered[5]))
    assert "no human decision" in " ".join(strings(rendered[1])).lower()
    row["disinformation"] = 0.95
    download = next(
        entry["callback"].__wrapped__
        for key, entry in app.callback_map.items()
        if "al-download.data" in key
    )
    fingerprint = _snapshot_fingerprint(case_data, AlertSettings.defaults())
    row["text"] = "changed after preview"
    result, message = download(1, ["one"], "case-a", fingerprint)
    assert result is no_update and "changed" in message
    fingerprint = _snapshot_fingerprint(case_data, AlertSettings.defaults())
    result, message = download(1, ["one", "missing"], "case-a", fingerprint)
    assert result is no_update and "selection changed" in message.lower()
    case_data.provenance = {}
    result, message = download(1, ["one"], "case-a", fingerprint)
    assert result is no_update and "captures" in message.lower()
    case_data.provenance = {"one": {"raw": raw, "run": None}}
    result, message = download(1, ["one"], "case-a", fingerprint)
    assert result["filename"].endswith(".pdf")
    assert "1 evidence items" in message


def test_case_guide_marks_every_supplied_example_unsupported():
    from pathlib import Path

    from dash.development.base_component import Component

    from evidence_explorer.policies.ui import _case_guide

    case = json.loads(
        (
            Path(__file__).resolve().parents[3] / "policy-guidelines/cases/zohran-mamdani.json"
        ).read_text()
    )
    panel = _case_guide(case, {"id": "twitter"})

    def strings(node):
        if isinstance(node, str):
            yield node
        elif isinstance(node, (list, tuple)):
            for child in node:
                yield from strings(child)
        elif isinstance(node, Component):
            yield from strings(getattr(node, "children", None))

    text = list(strings(panel))
    count = sum(len(category.get("examples", [])) for category in case["categories"])
    assert text.count("Unsupported, unreviewed example: source unavailable") == count
    assert text.count("Supplied interpretation (unverified): ") == count
    assert not any(point in text for point in case["strategy"]["points"])


@pytest.mark.parametrize("origin", [None, "", "unknown", "unclear"])
def test_unknown_attribution_is_visible_and_needs_review(origin):
    row, _ = _post()
    row["attribution"] = origin
    assessment = analysis.assess(row, AlertSettings.defaults(), {"ref": _reference()})
    assert assessment is not None and assessment.status == analysis.NEEDS
    check = next(c for c in assessment.checks if c.key == "attribution")
    assert not check.ok and "unknown" in check.detail


def test_ambiguous_labels_for_same_record_are_unavailable():
    from evidence_explorer.alerts.repository import AnalyticsUnavailable

    class DuplicateConnection(_Connection):
        def execute(self, query, args=None):
            result = super().execute(query, args)
            if "FROM analytics_content_labels l" in query:
                result.rows.append(dict(result.rows[0]))
            return result

    repo = AnalyticsRepository()
    repo._connection = DuplicateConnection
    with pytest.raises(AnalyticsUnavailable, match="Multiple labels"):
        repo.case_snapshot("case-a", ["one"])


def test_unknown_engagement_and_repeated_targeting_are_preserved():
    from evidence_explorer.alerts.pdf_report import _known_views, _number

    assert _number(None) == "Unknown"
    assert _number(0) == "0"
    assert _known_views([{"reach": {"views": None}}]) == ("Recorded views unknown; 1 posts unknown")
    assert _known_views([{"reach": {"views": None}}, {"reach": {"views": 5}}]) == (
        "5 known recorded views; 1 posts unknown"
    )
    row, raw = _post()
    a = analysis.assess(row, AlertSettings.defaults(), {"ref": _reference()})
    assert evidence_item(a, {"ref": _reference()}, raw)["pattern"]["repeated_targeting"] is None


@pytest.mark.parametrize("platform", [None, "", "unknown", "tiktok"])
def test_non_x_or_unknown_platform_is_visible_as_needing_review(platform):
    row, _ = _post()
    row["platform"] = platform
    a = analysis.assess(row, AlertSettings.defaults(), {"ref": _reference()})
    assert a is not None and a.status == analysis.NEEDS
    assert not next(c for c in a.checks if c.key == "platform").ok


@pytest.mark.parametrize("text", [None, "", "   "])
def test_missing_extracted_text_needs_review(text):
    row, _ = _post()
    row["text"] = text
    a = analysis.assess(row, AlertSettings.defaults(), {"ref": _reference()})
    assert a is not None and a.status == analysis.NEEDS
    assert not next(c for c in a.checks if c.key == "text").ok


def test_pdf_rechecks_candidate_instead_of_trusting_stale_status():
    from evidence_explorer.alerts.pdf_report import build_pdf

    row, raw = _post()
    refs = {"ref": _reference()}
    settings = AlertSettings.defaults()
    assessment = analysis.assess(row, settings, refs)
    row["text"] = ""
    with pytest.raises(ValueError, match="supported X platform"):
        build_pdf({"id": "case-a"}, [assessment], refs, {"one": {"raw": raw}}, [], settings)


def test_matching_handles_do_not_transfer_burst_findings_between_accounts():
    first, _ = _post()
    second = dict(first, id="two", author_id="other-author")
    refs = {"ref": _reference()}
    settings = AlertSettings.defaults()
    assessed = analysis.assess_all([first, second], settings, refs)
    grouped = analysis.accounts(assessed, settings, [{"author_handle": "someone"}])
    assert len(grouped) == 2
    assert all(account["bursts"] is None for account in grouped)


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "https://user:secret@example.org/", "https://example.org/\npath", None],
)
def test_unsafe_source_links_are_unavailable(url):
    from dash import html

    from evidence_explorer.alerts.ui import _link
    from evidence_explorer.policies.ui import _a

    row, _ = _post()
    row["url"] = url
    a = analysis.assess(row, AlertSettings.defaults(), {"ref": _reference()})
    assert a.status == analysis.NEEDS
    assert not next(c for c in a.checks if c.key == "location").ok
    assert isinstance(_link(url), html.Span)
    assert isinstance(_a("source", url), html.Span)


def test_valid_http_source_link_remains_available():
    from dash import html

    from evidence_explorer.alerts.ui import _link
    from evidence_explorer.policies.ui import _a

    url = "https://example.org/post"
    row, _ = _post()
    assert next(
        c
        for c in analysis.assess(row, AlertSettings.defaults(), {"ref": _reference()}).checks
        if c.key == "location"
    ).ok
    assert isinstance(_link(url), html.A)
    assert isinstance(_a("source", url), html.A)
