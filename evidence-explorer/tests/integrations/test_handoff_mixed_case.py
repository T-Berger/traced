"""Mixed-run handoffs validate captured native evidence and retain selected review scope."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from evidence_explorer.config import Settings
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.controller import select_assessment, session_for_case
from evidence_explorer.review.integrations import handoff_service
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService

PACKAGE = Path(__file__).resolve().parents[1] / "fixtures/case_packages/fictional-core-demo.zip"
PACKAGE_SHA256 = "c1d742a78db0b5043b79be6a3a878e1406569b7ad45d46513876791daf28fd7c"


def workspace(path):
    settings = Settings(data_dir=path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    return WorkspaceService(repository, settings, path / "artifacts")


@pytest.fixture
def desk(tmp_path):
    raw = PACKAGE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PACKAGE_SHA256
    service = workspace(tmp_path / "sender")
    case_id = service.import_package(raw)
    bundle = service.repository.get_bundle(case_id)
    media_run = next(
        run for run in bundle.runs if run.method == "supplied-media-metadata-and-byte-hashes"
    )
    media_finding = next(row for row in bundle.findings if row.run_id == media_run.id)
    media_assessment = service.start_scenario(case_id, media_finding.snapshot_id)
    return service, case_id, media_assessment


def selected_state(desk, selection):
    service, case_id, media_assessment = desk
    state = session_for_case(service, case_id)
    if selection == "media":
        select_assessment(service, state, media_assessment["id"])
    return state


def table_hashes(service):
    result = {}
    with sqlite3.connect(f"file:{service.settings.db_path}?mode=ro", uri=True) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        for (name,) in tables:
            cursor = connection.execute('SELECT * FROM "' + name.replace('"', '""') + '"')
            rows = sorted(
                json.dumps(list(row), default=lambda value: {"bytes": value.hex()})
                for row in cursor.fetchall()
            )
            payload = json.dumps(
                {"columns": [column[0] for column in cursor.description], "rows": rows}
            ).encode()
            result[name] = {"rows": len(rows), "sha256": hashlib.sha256(payload).hexdigest()}
    return result


def prepare(service, state, kind, **kwargs):
    if kind == "dossier":
        return handoff_service.prepare_dossier(service, state, **kwargs)
    return handoff_service.prepare_export(service, kind, state, **kwargs)


@pytest.mark.parametrize(("selection", "finding_count"), [("baseline", 2), ("media", 3)])
def test_availability_receives_complete_capture_from_one_read_transaction(
    desk, monkeypatch, selection, finding_count
):
    service, case_id, _ = desk
    state = selected_state(desk, selection)
    expected = service.capture_case_package(case_id).bundle
    before = table_hashes(service)
    original_connect = sqlite3.connect
    original_capture = WorkspaceService.capture_case_package
    original_availability = handoff_service.check_artifact_availability
    connections, statements, captures, availability_calls = [], [], [], []

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        connection.set_trace_callback(statements.append)
        return connection

    def capture(snapshot, selected_case):
        captured = original_capture(snapshot, selected_case)
        captures.append(captured)
        return captured

    def availability(current_service, bundle):
        assert len(captures) == 1
        assert bundle is captures[0].bundle
        assert isinstance(bundle, EvidenceBundle)
        assert len(bundle.runs) == 2 and len(bundle.findings) == 5
        assert bundle == expected
        assert "COMMIT" in statements, "File checks must use detached evidence after capture"
        availability_calls.append(bundle)
        return original_availability(current_service, bundle)

    def no_live_bundle_reread(*args, **kwargs):
        raise AssertionError("Artifact availability must not reload the live repository")

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", traced_connect)
        patch.setattr(WorkspaceService, "capture_case_package", capture)
        patch.setattr(handoff_service, "check_artifact_availability", availability)
        patch.setattr(service.repository, "get_bundle", no_live_bundle_reread)
        view, handoff, _ = handoff_service.load_handoff_context(service, state)

    assert len(connections) == 1
    assert statements.count("BEGIN") == statements.count("COMMIT") == 1
    assert len(availability_calls) == 1
    assert len(view["bundle"]["findings"]) == finding_count
    assert len(view["bundle"]["runs"]) == 2
    assert view["bundle"]["links"] == expected.model_dump(mode="json")["links"]
    assert handoff["counts"]["findings"] == finding_count
    assert handoff["counts"]["available_media"] == 3
    assert handoff["counts"]["unavailable_media"] == handoff["counts"]["unknown_media"] == 0
    assert handoff["export_allowed"] is True
    assert handoff["brief"]["included"] == []
    assert len(handoff["brief"]["omitted"]) == finding_count
    assert table_hashes(service) == before


@pytest.mark.parametrize("selection", ["baseline", "media"])
def test_selected_brief_and_complete_native_media_history_survive_export_and_reimport(
    desk, tmp_path, selection
):
    service, case_id, _ = desk
    state = selected_state(desk, selection)
    expected = service.capture_case_package(case_id)
    assert len(expected.bundle.runs) == 2 and len(expected.bundle.findings) == 5
    assert len(expected.bundle.scenarios) == 1
    assert not expected.bundle.reviews and expected.planning_history is None
    before = table_hashes(service)
    context = handoff_service.load_handoff_context(service, state)
    exports = {
        kind: prepare(service, state, kind, expected_context=context[1]["download_context"])
        for kind in ("package", "json", "brief", "dossier")
    }
    assert EvidenceBundle.model_validate_json(exports["json"].content) == expected.bundle
    selected_ids = {row["snapshot_id"] for row in context[0]["bundle"]["findings"]}
    for finding in expected.bundle.findings:
        if finding.snapshot_id in selected_ids:
            assert finding.snapshot_id in exports["brief"].content
        else:
            assert finding.snapshot_id not in exports["brief"].content
    recipient = workspace(tmp_path / "recipient")
    assert recipient.import_package(exports["package"].content) == case_id
    imported_before = table_hashes(recipient)
    restored = recipient.capture_case_package(case_id)
    assert restored.bundle == expected.bundle
    assert restored.planning_history is None
    media = [record for record in restored.bundle.records if record.artifact_ref]
    assert len(media) == 3
    for record in media:
        artifact = recipient.artifact(case_id, record.id, bundle=restored.bundle)
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == record.content_hash
    assert table_hashes(recipient) == imported_before
    assert table_hashes(service) == before
    assert hashlib.sha256(PACKAGE.read_bytes()).hexdigest() == PACKAGE_SHA256


def test_truly_unresolved_native_reference_still_rejects_without_changing_saved_tables(desk):
    service, case_id, _ = desk
    payload = service.capture_case_package(case_id).bundle.model_dump(mode="json")
    linked = next(link for link in payload["links"] if link["target"]["type"] == "finding")
    linked["target"]["id"] = "missing:true-finding"
    before = table_hashes(service)
    for validate in (
        EvidenceBundle.model_validate,
        service.import_bundle,
        lambda data: handoff_service.check_artifact_availability(service, data),
    ):
        with pytest.raises(ValueError, match="Unresolved finding reference"):
            validate(payload)
    assert table_hashes(service) == before


@pytest.mark.parametrize("dirty_kind", ["review", "planning"])
def test_unsaved_review_and_planning_guards_still_block_every_format_without_media_reads(
    desk, monkeypatch, dirty_kind
):
    service, case_id, _ = desk
    state = selected_state(desk, "media")
    context = handoff_service.load_handoff_context(service, state)
    kwargs = (
        {"note": "Unsaved reviewer qualification."}
        if dirty_kind == "review"
        else {"planning_draft": {"case_id": case_id, "dirty": True, "brief_id": None}}
    )
    before = table_hashes(service)

    def no_media_reads(*args, **kwargs):
        raise AssertionError("Dirty handoffs must not perform artifact availability reads")

    with monkeypatch.context() as patch:
        patch.setattr(handoff_service, "check_artifact_availability", no_media_reads)
        dirty = handoff_service.load_handoff_context(service, state, **kwargs)[1]
        assert dirty["dirty"] is True and dirty["export_allowed"] is False
        for kind in ("package", "json", "brief", "dossier"):
            with pytest.raises(ValueError, match="Unsaved"):
                prepare(
                    service, state, kind, expected_context=context[1]["download_context"], **kwargs
                )
    assert table_hashes(service) == before


@pytest.mark.parametrize("guard", ["absent", "stale-assessment"])
def test_displayed_context_guard_still_applies_to_every_mixed_case_export(desk, guard):
    service, _, _ = desk
    baseline = selected_state(desk, "baseline")
    context = handoff_service.load_handoff_context(service, baseline)
    state = selected_state(desk, "media") if guard == "stale-assessment" else baseline
    expected = context[1]["download_context"] if guard == "stale-assessment" else None
    before = table_hashes(service)
    for kind in ("package", "json", "brief", "dossier"):
        with pytest.raises(ValueError, match="Refresh handoff"):
            prepare(service, state, kind, expected_context=expected)
    assert table_hashes(service) == before


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_unavailable_media_remains_explicit_and_portable_package_is_rejected(desk, damage):
    service, case_id, _ = desk
    saved = service.capture_case_package(case_id).bundle
    record = next(row for row in saved.records if row.artifact_ref)
    artifact = service.artifact(case_id, record.id, bundle=saved)
    if damage == "missing":
        artifact.unlink()
    else:
        artifact.write_bytes(b"changed local file")
    state = selected_state(desk, "baseline")
    before = table_hashes(service)
    context = handoff_service.load_handoff_context(service, state)
    assert context[1]["counts"]["unavailable_media"] == 1
    assert context[1]["counts"]["available_media"] == 2
    with pytest.raises(ValueError, match="artifact|Artifact"):
        prepare(service, state, "package", expected_context=context[1]["download_context"])
    assert table_hashes(service) == before
