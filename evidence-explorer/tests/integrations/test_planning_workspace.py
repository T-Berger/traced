"""Planning survives native workspace transport without changing evidence or review meaning."""

import hashlib
import sqlite3
from pathlib import Path

import pytest

from evidence_explorer.config import Settings
from evidence_explorer.planning_briefs.models import DraftFields
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.integrations.packages import read_case_package
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.use_cases import project_case_progress


def workspace(path: Path):
    settings = Settings(data_dir=path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    return WorkspaceService(repository, settings, path / "artifacts")


def confirmed_plan(service, bundle=None):
    bundle = bundle or load_demo_bundle()
    service.repository.import_bundle(bundle)
    planner = service.planning_briefs
    binding = planner.case_binding(bundle.case.id)
    fields = DraftFields(
        use_case_id="cited_claim_investigation",
        purpose="Inspect the supplied public claim and its exact saved evidence.",
        public_subjects_or_topics=("Fictional bridge claim",),
        source_selection=binding.scope.source_refs,
        reviewer_role="Research reviewer",
        expected_output="reviewed_brief",
    )
    draft = planner.create_draft(author="Fixture analyst", fields=fields)
    bound = planner.bind(
        draft.brief_id,
        bundle.case.id,
        expected_head_revision_id=draft.revision_id,
        expected_case_fingerprint=bundle.fingerprint(),
        author="Fixture analyst",
    )
    confirmation = planner.confirm(
        draft.brief_id,
        expected_head_revision_id=bound.revision_id,
        expected_case_fingerprint=bundle.fingerprint(),
        reviewer="Fixture analyst",
    )
    return bundle, bound, confirmation


def test_empty_draft_reopens_without_creating_evidence_or_work(tmp_path):
    original = workspace(tmp_path / "desk")
    draft = original.planning_briefs.create_draft(author="Local analyst")
    reopened = workspace(tmp_path / "desk")
    assert reopened.planning_briefs.load(draft.brief_id).revision == draft
    with reopened.repository._connection() as connection:
        for table in (
            "review_cases",
            "review_scenarios",
            "review_jobs",
            "review_events",
            "collection_jobs",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_current_saved_scope_drives_progress_without_mutating_native_bundle(tmp_path):
    service = workspace(tmp_path)
    bundle, bound, _ = confirmed_plan(service)
    view = service.view(bundle.case.id)
    facts = service.planning_progress_facts(view)
    progress = project_case_progress("cited_claim_investigation", view, facts=facts)
    assert next(step for step in progress.steps if step.id == "scope").status == "completed"
    assert facts.brief_binding.revision_id == bound.revision_id
    assert service.repository.get_bundle(bundle.case.id).fingerprint() == bundle.fingerprint()
    assert service.repository.list_events(bundle.case.id) == []


def test_edit_keeps_older_confirmation_but_removes_current_progress(tmp_path):
    service = workspace(tmp_path)
    bundle, bound, confirmation = confirmed_plan(service)
    edited = service.planning_briefs.save_draft(
        bound.brief_id,
        bound.fields.model_copy(update={"purpose": "A changed public question."}),
        base_revision_id=bound.revision_id,
        expected_head_revision_id=bound.revision_id,
        author="Second editor",
    )
    assert edited.parent_revision_id == bound.revision_id
    assert service.planning_briefs.load(bound.brief_id).status == "needs_review"
    assert service.planning_progress_facts(service.view(bundle.case.id)).brief is None
    assert confirmation in service.planning_briefs.history(bound.brief_id).confirmations


def test_two_case_plans_and_package_lineages_stay_separate(tmp_path):
    service = workspace(tmp_path / "desk")
    first, first_plan, _ = confirmed_plan(service)
    second_data = first.model_dump(mode="json")
    second_data["case"]["id"] = "second:case"
    second_data["case"]["title"] = "Second supplied investigation"
    second = EvidenceBundle.model_validate(second_data)
    _, second_plan, _ = confirmed_plan(service, second)
    for case, expected in ((first, first_plan), (second, second_plan)):
        state = service.planning_briefs.state_for_case(case.case.id)
        assert state.revision == expected
        parsed = read_case_package(service.export_package(case.case.id), tmp_path / "receiving")
        assert {row.brief_id for row in parsed.planning_history.revisions} == {expected.brief_id}
    assert service.repository.get_bundle(first.case.id).fingerprint() == first.fingerprint()


def test_exact_portable_history_does_not_fabricate_a_local_confirmation(tmp_path):
    source = workspace(tmp_path / "source")
    bundle, bound, _ = confirmed_plan(source)
    source.planning_briefs.create_draft(author="Unrelated workspace draft")
    before = source.planning_briefs.history_for_case(bundle.case.id)
    package = source.export_package(bundle.case.id)
    receiver = workspace(tmp_path / "receiver")
    assert receiver.import_package(package) == bundle.case.id
    assert receiver.planning_briefs.history_for_case(bundle.case.id) == before
    assert receiver.repository.get_bundle(bundle.case.id).fingerprint() == bundle.fingerprint()
    assert receiver.planning_briefs.state_for_case(bundle.case.id) is None
    imported = receiver.planning_briefs.load(bound.brief_id, bound.revision_id)
    assert imported.imported and not imported.is_active_for_case
    assert not imported.confirmation_is_local and imported.head_revision_id is None
    assert receiver.planning_progress_facts(receiver.view(bundle.case.id)).brief is None
    assert receiver.import_package(package) == bundle.case.id
    assert receiver.planning_briefs.history_for_case(bundle.case.id) == before
    adopted = receiver.planning_briefs.adopt_revision(
        bound.revision_id,
        bundle.case.id,
        expected_head_revision_id=None,
        expected_case_fingerprint=bundle.fingerprint(),
        author="Receiving analyst",
    )
    assert adopted.revision_id != bound.revision_id
    assert receiver.planning_briefs.state_for_case(bundle.case.id).status == "needs_review"
    receiver.planning_briefs.confirm(
        adopted.brief_id,
        expected_head_revision_id=adopted.revision_id,
        expected_case_fingerprint=bundle.fingerprint(),
        reviewer="Receiving analyst",
    )
    assert receiver.planning_progress_facts(receiver.view(bundle.case.id)).brief is not None
    assert receiver.repository.list_events(bundle.case.id) == []


def test_review_history_does_not_rebind_confirmed_source_scope(tmp_path):
    service = workspace(tmp_path)
    bundle, bound, _ = confirmed_plan(service)
    scenario = service.repository.create_scenario(bundle.case.id, bundle.runs[0].id)
    exported = service.capture_case_package(bundle.case.id)
    assert exported.bundle.fingerprint() != bundle.fingerprint()
    exported_binding = next(
        row.binding
        for row in exported.planning_history.revisions
        if row.revision_id == bound.revision_id
    )
    assert exported_binding == bound.binding
    assert exported_binding.source_case_fingerprint == bundle.fingerprint()
    assert (
        service.planning_progress_facts(service.view(bundle.case.id, scenario["id"])).brief
        is not None
    )


def test_import_rolls_back_native_case_when_planning_storage_fails(tmp_path):
    source = workspace(tmp_path / "source")
    bundle, _, _ = confirmed_plan(source)
    package = source.export_package(bundle.case.id)
    receiver = workspace(tmp_path / "receiving")
    with receiver.repository._connection(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER reject_planning BEFORE INSERT ON monitoring_brief_revisions "
            "BEGIN SELECT RAISE(ABORT,'Planning write rejected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="Planning write rejected"):
        receiver.import_package(package)
    assert receiver.list_cases() == []
    assert receiver.planning_briefs.list() == ()
    assert not receiver.artifact_root.exists()


def test_commit_failure_rolls_back_case_history_and_only_new_media(tmp_path, monkeypatch):
    from evidence_explorer.review.integrations import workspace as workspace_module

    source = workspace(tmp_path / "source")
    data = load_demo_bundle().model_dump(mode="json")
    media = {"media/kept.svg": b"<svg>kept</svg>", "media/new.svg": b"<svg>new</svg>"}
    for reference, content in media.items():
        path = source.artifact_root / reference
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        data["records"].append(
            {
                "id": reference,
                "kind": "image",
                "source_label": "Synthetic commit-failure fixture",
                "artifact_ref": reference,
                "content_hash": hashlib.sha256(content).hexdigest(),
            }
        )
    bundle, _, _ = confirmed_plan(source, EvidenceBundle.model_validate(data))
    package = source.export_package(bundle.case.id)
    receiver = workspace(tmp_path / "receiver")
    kept = receiver.artifact_root / "media/kept.svg"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(media["media/kept.svg"])
    with receiver.repository._connection(write=True) as connection:
        connection.execute("CREATE TABLE commit_parent (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE commit_child (parent_id INTEGER REFERENCES commit_parent(id) "
            "DEFERRABLE INITIALLY DEFERRED)"
        )
        connection.execute(
            "CREATE TRIGGER reject_case_commit AFTER INSERT ON review_cases "
            "BEGIN INSERT INTO commit_child VALUES (1); END"
        )
    installed = []
    original = workspace_module.install_case_artifacts

    def observe_install(parsed, root):
        created = original(parsed, root)
        installed.extend(created)
        assert all(path.exists() for path in created)
        return created

    monkeypatch.setattr(workspace_module, "install_case_artifacts", observe_install)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        receiver.import_package(package)
    assert installed == [receiver.artifact_root / "media/new.svg"]
    assert not installed[0].exists()
    assert kept.read_bytes() == media["media/kept.svg"]
    assert receiver.list_cases() == []
    assert receiver.planning_briefs.list() == ()
