"""Saved investigator actions preserve snapshots, exact quotes, and human decisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace

import pytest

from evidence_explorer.config import Settings
from evidence_explorer.investigation import workspace
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService

TEXT = "domainName: example.org\ncreationDate: 2007-10-09\nregistrar: Example Registrar\n"
QUOTE = "creationDate: 2007-10-09"


def fixture_bundle():
    return {
        "schema_version": "1.0",
        "case": {
            "id": "fixture-case",
            "title": "Saved public record",
            "question": "When was it created?",
            "synthetic": True,
        },
        "records": [
            {
                "id": "record-1",
                "kind": "saved_source_item",
                "source_label": "Fictional source",
                "text": TEXT,
                "content_hash": hashlib.sha256(TEXT.encode()).hexdigest(),
            }
        ],
        "fragments": [],
        "runs": [
            {
                "id": "source-run",
                "input_fingerprint": "fixture",
                "input_manifest": ["record-1"],
                "method": "saved_source_collection",
                "method_version": "1",
                "created_at": "2026-11-07T00:00:00Z",
            }
        ],
        "findings": [
            {
                "snapshot_id": "source-finding",
                "lineage_id": "source-finding",
                "version": 1,
                "run_id": "source-run",
                "kind": "observation",
                "statement": "One saved public record.",
            }
        ],
        "links": [],
        "reviews": [
            {
                "id": "imported-review",
                "finding_snapshot_id": "source-finding",
                "run_id": "source-run",
                "reviewer": "Fixture reviewer",
                "disposition": "qualify",
                "reason": "Fictional exercise only.",
                "timestamp": "2026-11-07T00:00:01Z",
            }
        ],
        "scenarios": [],
    }


@pytest.fixture
def desk(tmp_path):
    settings = Settings(data_dir=tmp_path, ollama_url="http://127.0.0.1:1")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    service.import_bundle(fixture_bundle())
    return service


def state(desk):
    return session_for_case(desk, "fixture-case")


def draft(desk, selected=None, **changes):
    values = {
        "claim": "example.org was created in 2007.",
        "step": "The creation date is 2007-10-09.",
        "record_id": "record-1",
        "quote": QUOTE,
    }
    values.update(changes)
    return workspace.save_manual_draft(desk, selected or state(desk), **values)


def rows(desk):
    with sqlite3.connect(desk.repository.db_path) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return {
            name: connection.execute(
                'SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid'
            ).fetchall()
            for (name,) in tables
        }


def save_folder(desk, name="20261107T000000Z-fixture"):
    draft(desk)
    bundle = desk.get_case("fixture-case")
    folder = desk.settings.data_dir / "investigations" / name
    folder.mkdir(parents=True)
    (folder / "bundle.json").write_text(json.dumps(bundle))
    (folder / "investigation.json").write_text(
        json.dumps(
            {
                "schema": "traced.investigation/1",
                "bundle": {
                    "path": "bundle.json",
                    "fingerprint": EvidenceBundle.model_validate(bundle).fingerprint(),
                },
            }
        )
    )
    return folder


def test_manual_draft_preserves_original_objects_and_opens_its_exact_run(desk):
    before = desk.get_case("fixture-case")
    result = draft(desk, assumption="The source describes this domain's original registration.")
    after = desk.get_case("fixture-case")
    for name, identity in (
        ("records", "id"),
        ("runs", "id"),
        ("findings", "snapshot_id"),
        ("reviews", "id"),
    ):
        saved = {item[identity]: item for item in after[name]}
        assert all(saved[item[identity]] == item for item in before[name])
    assert after["runs"][0] == before["runs"][0]
    run = next(item for item in after["runs"] if item["id"] == result["run_id"])
    assert run["configuration"]["draft_source"] == "manual"
    assert after["reviews"] == before["reviews"]
    view = desk.view(result["case_id"], result["session"]["scenario_id"])
    assert view["scenario"]["base_run_id"] == result["run_id"]
    assert {finding["run_id"] for finding in view["bundle"]["findings"]} == {result["run_id"]}
    assert any(fragment["quote"] == QUOTE for fragment in after["fragments"])
    assert (
        workspace.open_investigation_session(desk, "fixture-case")["finding_id"]
        == result["finding_id"]
    )


@pytest.mark.parametrize("quote", ["not in the source", "", "  creationDate: 2007-10-09"])
def test_invalid_quote_never_creates_a_run_or_review(desk, quote):
    before = rows(desk)
    with pytest.raises(ValueError):
        draft(desk, quote=quote)
    assert rows(desk) == before


def test_check_uses_detached_snapshot_and_does_not_write(desk, monkeypatch):
    first = draft(desk)
    latest = draft(desk, first["session"], claim="The source reports a 2007 creation date.")
    original = workspace.workspace_snapshot
    snapshots = []

    @contextmanager
    def observed(service):
        with original(service) as captured:
            snapshots.append(captured.repository)
            yield captured

    actual = workspace.check_bundle

    def checked(bundle, **kwargs):
        assert snapshots and not snapshots[-1]._snapshot_active
        assert kwargs["reasoner"].verifier is None
        return actual(bundle, **kwargs)

    monkeypatch.setattr(workspace, "workspace_snapshot", observed)
    monkeypatch.setattr(workspace, "check_bundle", checked)
    before = rows(desk)
    result = workspace.check_case(desk, latest["session"])
    assert rows(desk) == before
    assert result["context"]["run_id"] == latest["run_id"]
    assert result["report"]["summary"]["model_checks"]["status"] == "not_run"
    assert [item["id"] for item in result["report"]["conclusions"]] == [latest["finding_id"]]
    assert first["finding_id"] not in {item["id"] for item in result["report"]["findings"]}


def test_stale_case_and_different_case_context_reject(desk):
    old = state(desk)
    old_context = workspace.check_context(desk, old)
    saved = draft(desk, old)
    with pytest.raises(ValueError, match="changed"):
        workspace.check_case(desk, old)
    with pytest.raises(ValueError, match="changed"):
        workspace.check_case(desk, saved["session"], expected_context=old_context)
    other = fixture_bundle()
    other["case"]["id"] = "another-case"
    desk.import_bundle(other)
    with pytest.raises(ValueError, match="changed"):
        workspace.check_case(
            desk,
            session_for_case(desk, "another-case"),
            expected_context=workspace.check_context(desk, saved["session"]),
        )


def test_check_rejects_case_changed_during_computation(desk, monkeypatch):
    saved = draft(desk)
    actual = workspace.check_bundle

    def changed(bundle, **kwargs):
        result = actual(bundle, **kwargs)
        update = deepcopy(bundle)
        update["runs"].append({**update["runs"][0], "id": "concurrent-run"})
        desk.import_bundle(update)
        return result

    monkeypatch.setattr(workspace, "check_bundle", changed)
    with pytest.raises(ValueError, match="changed"):
        workspace.check_case(desk, saved["session"])


def test_exclusions_are_unavailable_not_counterfactual_checks(desk):
    saved = draft(desk)
    selected = saved["session"]
    scenario = desk.start_scenario_state(selected["case_id"], selected["finding_id"])
    selected.update(scenario_id=scenario.id, scenario_version=scenario.version)
    scenario = desk.exclude(
        selected["scenario_id"], "record-1", True, "Fixture exclusion", selected["scenario_version"]
    )
    selected["scenario_version"] = scenario["version"]
    before = rows(desk)
    with pytest.raises(ValueError, match="does not recompute"):
        workspace.check_case(desk, selected)
    with pytest.raises(ValueError, match="does not recompute"):
        draft(desk, selected)
    assert rows(desk) == before


def test_manual_save_revalidates_after_build_before_atomic_import(desk, monkeypatch):
    actual = workspace.build_saved_investigation_bundle

    def changed(bundle, *args, **kwargs):
        built = actual(bundle, *args, **kwargs)
        update = deepcopy(bundle)
        update["runs"].append({**update["runs"][0], "id": "concurrent-run"})
        desk.import_bundle(update)
        return built

    monkeypatch.setattr(workspace, "build_saved_investigation_bundle", changed)
    with pytest.raises(ValueError, match="changed"):
        draft(desk)
    assert not any(
        run["method"] == "traced_investigator" for run in desk.get_case("fixture-case")["runs"]
    )


def test_saved_inventory_preview_and_import_preserve_decisions(desk):
    folder = save_folder(desk)
    before = rows(desk)
    inventory = workspace.list_saved_investigations(desk)
    assert inventory["unavailable"] == 0 and not inventory["truncated"]
    assert len(inventory["items"]) == 1
    summary = inventory["items"][0]
    assert summary["id"] == folder.name and summary["draft_source"] == "manual"
    assert summary["claims"] == 1 and summary["steps"] == 1
    assert rows(desk) == before
    assert (
        workspace.import_saved_investigation(
            desk, folder.name, expected_fingerprint=summary["fingerprint"]
        )
        == "fixture-case"
    )
    assert rows(desk) == before
    with pytest.raises(ValueError, match="changed"):
        workspace.import_saved_investigation(desk, folder.name, expected_fingerprint="stale")


@pytest.mark.parametrize(
    "kind",
    ["folder_symlink", "bundle_symlink", "oversized", "manifest_path", "fingerprint", "malformed"],
)
def test_unsafe_or_changed_local_drafts_are_unavailable_without_path_disclosure(
    desk, tmp_path, kind
):
    folder = save_folder(desk)
    if kind == "folder_symlink":
        renamed = tmp_path / "outside-fixture"
        folder.rename(renamed)
        folder.symlink_to(renamed, target_is_directory=True)
    elif kind == "bundle_symlink":
        outside = tmp_path / "outside.json"
        (folder / "bundle.json").rename(outside)
        (folder / "bundle.json").symlink_to(outside)
    elif kind == "oversized":
        with (folder / "bundle.json").open("wb") as stream:
            stream.truncate(workspace.MAX_BUNDLE_BYTES + 1)
    elif kind == "malformed":
        (folder / "bundle.json").write_text("{")
    else:
        manifest = json.loads((folder / "investigation.json").read_text())
        manifest["bundle"]["path" if kind == "manifest_path" else "fingerprint"] = "../outside.json"
        (folder / "investigation.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError) as error:
        workspace.preview_saved_investigation(desk, folder.name)
    assert str(tmp_path) not in str(error.value)
    result = workspace.list_saved_investigations(desk)
    assert not result["items"] and result["unavailable"] == 1


def test_catalog_traversal_and_interactive_limits(desk, monkeypatch):
    for value in ("../escape", "/tmp/escape", ".", "safe/../../escape"):
        with pytest.raises(ValueError):
            workspace.preview_saved_investigation(desk, value)
    monkeypatch.setattr(workspace, "MAX_CHECK_TEXT_BYTES", 5)
    with pytest.raises(ValueError, match="size limit"):
        workspace.check_case(desk, state(desk))


def test_read_only_workspace_can_check_but_cannot_save_or_import(desk):
    folder = save_folder(desk)
    saved = workspace.open_investigation_session(desk, "fixture-case")
    preview = workspace.preview_saved_investigation(desk, folder.name)
    desk.settings = replace(desk.settings, read_only=True)
    workspace.check_case(desk, saved)
    with pytest.raises(PermissionError):
        draft(desk, saved)
    with pytest.raises(PermissionError):
        workspace.import_saved_investigation(
            desk, folder.name, expected_fingerprint=preview["fingerprint"]
        )


def test_media_without_text_is_unavailable_not_a_corruption_verdict(desk):
    bundle = fixture_bundle()
    bundle["records"][0].update(text=None, artifact_ref="fixture.png")
    with pytest.raises(ValueError, match="without saved text"):
        workspace._bounded_check(bundle)
    workspace._bounded_check(bundle, require_text=False)


def test_shared_dependency_paths_are_bounded_before_checker_runs():
    bundle = fixture_bundle()
    bundle["findings"] = []
    bundle["links"] = []
    previous = []
    for level in range(18):
        current = [f"f-{level}-a", f"f-{level}-b"]
        for finding_id in current:
            bundle["findings"].append({"snapshot_id": finding_id})
            for source in previous:
                bundle["links"].append(
                    {
                        "relation": "supports",
                        "source": {"type": "finding", "id": source},
                        "target": {"type": "finding", "id": finding_id},
                    }
                )
        previous = current
    with pytest.raises(ValueError, match="work limit"):
        workspace._bounded_check(bundle)


def test_same_run_step_navigation_keeps_report_context(desk):
    result = draft(desk)
    selected = result["session"]
    context = workspace.check_context(desk, selected)
    step = next(
        item
        for item in desk.get_case("fixture-case")["findings"]
        if item["run_id"] == result["run_id"] and item["details"].get("role") == "step"
    )
    assert workspace.check_context(desk, {**selected, "finding_id": step["snapshot_id"]}) == context


def test_saved_catalog_limits_are_explicit(desk, monkeypatch):
    folder = save_folder(desk)
    other = folder.parent / "incomplete-draft"
    other.mkdir()
    monkeypatch.setattr(workspace, "MAX_SAVED_ENTRIES", 1)
    assert workspace.list_saved_investigations(desk)["truncated"]


def test_open_without_claim_leaves_storage_unchanged(desk):
    before = rows(desk)
    with pytest.raises(ValueError, match="no investigator claim"):
        workspace.open_investigation_session(desk, "fixture-case")
    assert rows(desk) == before


def test_open_previewed_older_run_does_not_jump_to_newer_draft(desk):
    older = draft(desk)
    newer = draft(desk, older["session"], claim="A newer draft of the claim.")
    reopened = workspace.open_investigation_session(desk, "fixture-case", run_id=older["run_id"])
    assert reopened["finding_id"] == older["finding_id"]
    assert workspace.check_context(desk, reopened)["run_id"] == older["run_id"]
    assert (
        workspace.open_investigation_session(desk, "fixture-case")["finding_id"]
        == newer["finding_id"]
    )
    with pytest.raises(ValueError, match="no investigator claim"):
        workspace.open_investigation_session(desk, "fixture-case", run_id="wrong-run")


@pytest.mark.parametrize("kind", ["image", "whole_record"])
def test_nontext_selectors_are_unavailable_even_when_record_has_text(kind):
    bundle = fixture_bundle()
    bundle["fragments"] = [{"selector": {"kind": kind}}]
    with pytest.raises(ValueError, match="whole-record or image"):
        workspace._bounded_check(bundle)


@pytest.mark.parametrize("unsafe", ["oversized", "symlink"])
def test_frozen_record_access_limits_are_unavailable_not_failed(desk, tmp_path, unsafe):
    bundle = fixture_bundle()
    bundle["records"][0]["metadata"] = {"platform": "maltego", "native_id": "a" * 64}
    bundle["case"]["id"] = "maltego-fixture"
    desk.import_bundle(bundle)
    records = tmp_path / "maltego" / "records"
    records.mkdir(parents=True)
    path = records / ("a" * 64 + ".json")
    if unsafe == "oversized":
        with path.open("wb") as stream:
            stream.truncate(workspace.MAX_FROZEN_BYTES + 1)
    else:
        target = tmp_path / "outside-record.json"
        target.write_text("private fixture content")
        path.symlink_to(target)
    report = workspace.check_case(desk, session_for_case(desk, "maltego-fixture"))["report"]
    record = report["records"][0]
    assert record["frozen"] == "unavailable"
    assert "interactive size and access limits" in record["detail"]
    assert report["summary"]["records_failed"] == 0
    assert "private fixture content" not in json.dumps(report)
