"""Planning handoffs retain native evidence and the exact previewed planning history."""

import base64
import json

import pytest

from evidence_explorer.config import Settings
from evidence_explorer.planning_briefs.models import DraftFields
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.integrations import handoff_service
from evidence_explorer.review.integrations.handoff_service import (
    import_receipt_for,
    load_handoff_context,
    prepare_export,
)
from evidence_explorer.review.integrations.packages import (
    FORMAT,
    PLANNING_FORMAT,
    read_case_package,
)
from evidence_explorer.review.integrations.web import create_app
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.navigation import navigation_context
from tests.integrations.test_handoff_flow import post_callback


def workspace(path):
    settings = Settings(data_dir=path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    return WorkspaceService(repository, settings, path / "artifacts")


@pytest.fixture
def desk(tmp_path):
    service = workspace(tmp_path / "sender")
    case_id = service.import_bundle(load_demo_bundle().model_dump(mode="json"))
    baseline = service.baseline_scenario(case_id, "harbor:c1-v1")
    service.review(
        case_id, "harbor:c1-v1", baseline["id"], "qualify", "Saved analyst qualification."
    )
    return service, case_id, session_for_case(service, case_id)


def create_draft(service, case_id):
    planner = service.planning_briefs
    fields = DraftFields(
        use_case_id="cited_claim_investigation",
        purpose="Review the supplied public harbor statement.",
        public_subjects_or_topics=("Harbor access",),
        source_selection=planner.case_binding(case_id).scope.source_refs,
        reviewer_role="Research analyst",
        expected_output="reviewed_brief",
    )
    return planner.create_draft(author="Mina, planning author", fields=fields)


def bind_draft(service, case_id, draft):
    return service.planning_briefs.bind(
        draft.brief_id,
        case_id,
        expected_head_revision_id=draft.revision_id,
        expected_case_fingerprint=service.case_fingerprint(case_id),
        author="Mina, planning author",
    )


def confirm_draft(service, case_id, revision):
    return service.planning_briefs.confirm(
        revision.brief_id,
        expected_head_revision_id=revision.revision_id,
        expected_case_fingerprint=service.case_fingerprint(case_id),
        reviewer="Jules, planning reviewer",
    )


def edit_draft(service, revision):
    return service.planning_briefs.save_draft(
        revision.brief_id,
        revision.fields.model_copy(update={"purpose": "Review the revised public question."}),
        base_revision_id=revision.revision_id,
        expected_head_revision_id=revision.revision_id,
        author="Rowan, second planning author",
    )


def change_planning(service, case_id, revision, change):
    if change == "first_binding":
        return bind_draft(service, case_id, revision)
    if change == "new_draft":
        return edit_draft(service, revision)
    assert change == "new_confirmation"
    return confirm_draft(service, case_id, revision)


def revision_before_change(service, case_id, change):
    revision = create_draft(service, case_id)
    if change != "first_binding":
        revision = bind_draft(service, case_id, revision)
    if change == "new_draft":
        confirm_draft(service, case_id, revision)
    return revision


def prepare(service, kind, state, **kwargs):
    if kind == "dossier":
        return handoff_service.prepare_dossier(service, state, **kwargs)
    return prepare_export(service, kind, state, **kwargs)


def test_case_without_relevant_planning_keeps_v1_and_native_evidence(desk, tmp_path):
    service, case_id, state = desk
    before = service.export_bundle(case_id)
    unrelated = service.planning_briefs.create_draft(author="Unrelated draft author")
    captured = service.capture_case_package(case_id)
    assert captured.planning_history is None
    assert captured.bundle == before
    prepared = prepare_export(service, "package", state)
    parsed = read_case_package(prepared.content, tmp_path / "parsed", install_artifacts=False)
    assert parsed.manifest.format == FORMAT
    assert parsed.planning_history is None
    assert parsed.bundle == before
    assert service.planning_briefs.load(unrelated.brief_id).revision == unrelated


def test_v2_case_preserves_native_bundle_and_attributed_planning_without_local_approval(
    desk, tmp_path
):
    service, case_id, state = desk
    native_before = service.export_bundle(case_id)
    original = bind_draft(service, case_id, create_draft(service, case_id))
    first_confirmation = confirm_draft(service, case_id, original)
    latest = edit_draft(service, original)
    second_confirmation = confirm_draft(service, case_id, latest)
    unrelated = service.planning_briefs.create_draft(author="Unrelated draft author")
    expected_history = service.planning_briefs.history_for_case(case_id)
    assert {row.brief_id for row in expected_history.revisions} == {original.brief_id}
    assert {row.confirmation_id for row in expected_history.confirmations} == {
        first_confirmation.confirmation_id,
        second_confirmation.confirmation_id,
    }
    captured = service.capture_case_package(case_id)
    assert captured.bundle == native_before
    assert captured.planning_history == expected_history
    prepared = prepare_export(service, "package", state)
    parsed = read_case_package(prepared.content, tmp_path / "parsed", install_artifacts=False)
    assert parsed.manifest.format == PLANNING_FORMAT
    assert parsed.bundle == native_before
    assert parsed.planning_history == expected_history

    recipient = workspace(tmp_path / "recipient")
    reopened_id = recipient.import_package(prepared.content)
    assert recipient.export_bundle(reopened_id) == native_before
    assert recipient.planning_briefs.history_for_case(reopened_id) == expected_history
    assert recipient.planning_briefs.state_for_case(reopened_id) is None
    imported = recipient.planning_briefs.load(latest.brief_id, latest.revision_id)
    assert imported.revision.author == "Rowan, second planning author"
    assert imported.confirmation.reviewer == "Jules, planning reviewer"
    assert imported.imported is True
    assert imported.confirmation_is_local is False
    assert imported.status == "needs_review"
    assert recipient.repository.list_events(reopened_id) == []
    assert unrelated.brief_id not in {
        row.revision.brief_id for row in recipient.planning_briefs.list()
    }
    receipt = import_receipt_for(recipient, reopened_id)
    planning = receipt["planning"]
    assert planning["status"] == "history_available"
    assert planning["case_id"] == case_id
    assert planning["counts"]["revisions"] == len(expected_history.revisions)
    assert planning["counts"]["confirmations"] == 2
    assert planning["current_local_state"] == "not_projected"
    assert {row["author"] for row in planning["revisions"]} == {
        "Mina, planning author",
        "Rowan, second planning author",
    }
    assert all(row["reviewer"] == "Jules, planning reviewer" for row in planning["confirmations"])
    assert all(row["grants_local_approval"] is False for row in planning["confirmations"])
    assert receipt["validation"]["recipient_acceptance"] == "not_recorded"


def test_evidence_json_preserves_native_data_and_explicitly_discloses_planning_omission(desk):
    service, case_id, state = desk
    revision = bind_draft(service, case_id, create_draft(service, case_id))
    confirm_draft(service, case_id, revision)
    native = service.export_bundle(case_id).model_dump(mode="json")
    context = load_handoff_context(service, state)
    prepared = prepare_export(
        service, "json", state, expected_context=context[1]["download_context"]
    )
    assert json.loads(prepared.content) == native
    assert "planning" in prepared.message.lower()
    assert "not included" in prepared.message.lower() or "omit" in prepared.message.lower()
    assert context[1]["planning"]["counts"]["revisions"] == 2


@pytest.mark.parametrize("kind", ["package", "json", "brief", "dossier"])
def test_download_requires_complete_matching_browser_preview_when_explicitly_supplied(desk, kind):
    service, case_id, state = desk
    context = load_handoff_context(service, state)
    preview = context[1]["download_context"]
    events = service.repository.list_events(case_id)
    for supplied in (
        None,
        {},
        {"assessment_binding": preview["assessment_binding"]},
        {**preview, "assessment_binding": "another-assessment"},
        {**preview, "planning_context_token": "another-planning-history"},
    ):
        with pytest.raises(ValueError, match="(?i)refresh"):
            prepare(service, kind, state, expected_context=supplied)
    assert service.repository.list_events(case_id) == events


@pytest.mark.parametrize("kind", ["package", "json", "brief", "dossier"])
@pytest.mark.parametrize("change", ["first_binding", "new_draft", "new_confirmation"])
def test_planning_changes_invalidate_browser_preview_without_changing_assessment(
    desk, kind, change
):
    service, case_id, state = desk
    revision = revision_before_change(service, case_id, change)
    context = load_handoff_context(service, state)
    preview = context[1]["download_context"]
    native = service.export_bundle(case_id)
    events = service.repository.list_events(case_id)
    change_planning(service, case_id, revision, change)
    assert navigation_context(service.view(case_id), state) == context[2]
    assert (
        service.planning_briefs.context_token_for_case(case_id) != preview["planning_context_token"]
    )

    with pytest.raises(ValueError, match="(?i)(planning|handoff).*changed|refresh"):
        prepare(service, kind, state, expected_context=preview)

    assert service.export_bundle(case_id) == native
    assert service.repository.list_events(case_id) == events


@pytest.mark.parametrize("change", ["first_binding", "new_draft", "new_confirmation"])
def test_previously_loaded_handoff_context_also_rejects_changed_planning(desk, change):
    service, case_id, state = desk
    revision = revision_before_change(service, case_id, change)
    context = load_handoff_context(service, state)
    change_planning(service, case_id, revision, change)
    assert service.view(case_id) == context[0]

    with pytest.raises(ValueError, match="(?i)(planning|handoff).*changed|refresh"):
        prepare_export(service, "package", state, context=context)


@pytest.mark.parametrize("kind", ["package", "json", "brief", "dossier"])
def test_other_finding_saved_decision_invalidates_browser_preview_without_navigation_change(
    desk, kind
):
    service, case_id, state = desk
    other_finding = "harbor:c2-v1"
    assert state["finding_id"] != other_finding
    baseline = service.baseline_scenario(case_id, state["finding_id"])
    context = load_handoff_context(service, state)
    service.review(
        case_id,
        other_finding,
        baseline["id"],
        "withhold",
        "A second analyst withheld the other finding.",
    )
    current = load_handoff_context(service, state)
    assert current[2] == context[2]
    assert (
        current[1]["download_context"]["planning_context_token"]
        == context[1]["download_context"]["planning_context_token"]
    )
    assert (
        current[1]["download_context"]["assessment_binding"]
        != context[1]["download_context"]["assessment_binding"]
    )
    events = service.repository.list_events(case_id)

    with pytest.raises(ValueError, match="(?i)refresh"):
        prepare(service, kind, state, expected_context=context[1]["download_context"])

    assert service.repository.list_events(case_id) == events


@pytest.mark.parametrize("kind", ["package", "json", "brief", "dossier"])
@pytest.mark.parametrize("same_case", [True, False], ids=["matching-case", "unrelated-case"])
def test_unsaved_planning_edit_blocks_only_its_own_case_without_creating_approval(
    desk, tmp_path, kind, same_case
):
    service, case_id, state = desk
    revision = bind_draft(service, case_id, create_draft(service, case_id))
    before = service.capture_case_package(case_id)
    events = service.repository.list_events(case_id)
    context = load_handoff_context(service, state)
    planning_draft = {
        "case_id": case_id if same_case else "another-saved-case",
        "brief_id": revision.brief_id,
        "revision_id": revision.revision_id,
        "expected_head_revision_id": revision.revision_id,
        "dirty": True,
    }
    guarded = load_handoff_context(service, state, planning_draft=planning_draft)
    assert guarded[1]["dirty"] is same_case
    assert guarded[1]["export_allowed"] is (not same_case)
    assert guarded[1]["planning"]["confirmations"] == []

    if same_case:
        with pytest.raises(ValueError, match="(?i)unsaved"):
            prepare(
                service,
                kind,
                state,
                planning_draft=planning_draft,
                expected_context=context[1]["download_context"],
            )
    else:
        prepared = prepare(
            service,
            kind,
            state,
            planning_draft=planning_draft,
            expected_context=context[1]["download_context"],
        )
        assert prepared.content
        if kind == "package":
            parsed = read_case_package(
                prepared.content, tmp_path / "parsed", install_artifacts=False
            )
            assert parsed.bundle == before.bundle
            assert parsed.planning_history == before.planning_history
        elif kind == "json":
            assert json.loads(prepared.content) == before.bundle.model_dump(mode="json")
    assert service.capture_case_package(case_id) == before
    assert service.repository.list_events(case_id) == events
    assert service.planning_briefs.load(revision.brief_id).status == "needs_review"


@pytest.mark.parametrize("kind", ["package", "json", "brief", "dossier"])
def test_registered_download_rejects_old_planning_preview_and_succeeds_after_refresh(
    desk, tmp_path, kind
):
    service, case_id, state = desk
    revision = bind_draft(service, case_id, create_draft(service, case_id))
    app = create_app(service)
    values = {
        "rv-session": state,
        "rv-draft-store": {},
        "rv-review-reason": "",
        "trail-use-case-purpose": {"purpose_id": "cited_claim_investigation"},
    }
    preview = post_callback(app, "trail-handoff-summary.children", "rv-session", values)
    values["trail-handoff-context"] = preview["trail-handoff-context"]["data"]
    assert set(values["trail-handoff-context"]) == {
        "assessment_binding",
        "planning_context_token",
    }
    assert preview["trail-dossier"]["disabled"] is False
    latest = edit_draft(service, revision)
    events = service.repository.list_events(case_id)

    stale = post_callback(app, "trail-download.data", f"trail-{kind}", values)
    assert "trail-download" not in stale
    assert "refresh" in json.dumps(stale).lower()

    refreshed = post_callback(
        app, "trail-handoff-summary.children", "trail-handoff-refresh", values
    )
    current = refreshed["trail-handoff-context"]["data"]
    assert current != values["trail-handoff-context"]
    values["trail-handoff-context"] = current
    response = post_callback(app, "trail-download.data", f"trail-{kind}", values)
    download = response["trail-download"]["data"]
    assert download["content"]
    if kind == "package":
        parsed = read_case_package(
            base64.b64decode(download["content"]),
            tmp_path / "parsed",
            install_artifacts=False,
        )
        assert parsed.manifest.format == PLANNING_FORMAT
        assert parsed.planning_history == service.planning_briefs.history_for_case(case_id)
        assert latest.revision_id in {row.revision_id for row in parsed.planning_history.revisions}
    elif kind == "json":
        assert json.loads(download["content"]) == service.export_bundle(case_id).model_dump(
            mode="json"
        )
    else:
        assert "Saved analyst qualification." in download["content"]
        if kind == "dossier":
            assert latest.revision_id in download["content"]
            assert latest.fields.purpose in download["content"]
    assert service.repository.list_events(case_id) == events


@pytest.mark.parametrize("change", ["first_binding", "new_draft", "new_confirmation"])
def test_competing_planning_commit_during_capture_keeps_previously_visible_history(
    desk, tmp_path, monkeypatch, change
):
    service, case_id, state = desk
    revision = revision_before_change(service, case_id, change)
    context = load_handoff_context(service, state)
    captured = service.capture_case_package(case_id)
    original_view = WorkspaceService.view
    committed = False

    def view_then_commit(snapshot, selected_case, scenario_id=None):
        nonlocal committed
        result = original_view(snapshot, selected_case, scenario_id)
        if selected_case == case_id and not committed:
            committed = True
            change_planning(service, case_id, revision, change)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(WorkspaceService, "view", view_then_commit)
        prepared = prepare_export(
            service, "package", state, expected_context=context[1]["download_context"]
        )

    assert committed, "The competing planning change must commit inside the capture window"
    current = service.capture_case_package(case_id)
    assert current.planning_context_token != captured.planning_context_token
    assert current.planning_history != captured.planning_history
    assert current.bundle == captured.bundle
    parsed = read_case_package(prepared.content, tmp_path / "parsed", install_artifacts=False)
    assert prepared.binding == context[2]
    assert parsed.bundle == captured.bundle
    assert parsed.planning_history == captured.planning_history
    assert parsed.manifest.format == (FORMAT if change == "first_binding" else PLANNING_FORMAT)
