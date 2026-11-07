"""Output-guide labels never invent local scope authority from transport history.

Every confirmation below is automated fixture practice, not human validation.
The constructed evidence is the existing explicitly synthetic demo bundle.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from plotly.utils import PlotlyJSONEncoder

from evidence_explorer.config import Settings
from evidence_explorer.planning_briefs.models import DraftFields
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.integrations.handoff_service import (
    load_handoff_context,
    prepare_dossier,
    prepare_export,
)
from evidence_explorer.review.integrations.handoff_ui import render_handoff
from evidence_explorer.review.integrations.packages import read_case_package
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService

KINDS = ("unbound_draft", "local_confirmation", "imported_history")
AUTHOR = "OUT-03 automated fixture author — not a human reviewer"
REVIEWER = "OUT-03 automated scope confirmation practice — not human validation"
GUIDE = "platform_evidence_dossier"
NOTE = (
    "This session-only output guide is separate from any saved investigation plan "
    "and attributed scope confirmations. It does not establish investigation scope, "
    "policy eligibility, or approval."
)


def workspace(path):
    settings = Settings(data_dir=path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    return WorkspaceService(repository, settings, path / "artifacts")


def table_snapshot(database):
    """Hash every row and column in one read-only transaction, without exclusions."""
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        result = {}
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ):
            quoted = '"' + name.replace('"', '""') + '"'
            rows = sorted(
                json.dumps(
                    [value.hex() if isinstance(value, bytes) else value for value in row],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                for row in connection.execute(f"SELECT * FROM {quoted}")
            )
            result[name] = {
                "count": len(rows),
                "sha256": hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
                "columns": [row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")],
            }
    return result


def ui_text(node):
    if isinstance(node, str):
        return node
    if isinstance(node, (list, tuple)):
        return " ".join(ui_text(child) for child in node)
    return ui_text(getattr(node, "children", ""))


def make_scope_fixture(path, kind):
    service = workspace(path / "author")
    case_id = service.import_bundle(load_demo_bundle().model_dump(mode="json"))
    planner = service.planning_briefs
    draft = planner.create_draft(
        author=AUTHOR,
        fields=DraftFields(
            use_case_id="cited_claim_investigation",
            purpose="Automated fixture: inspect the explicitly fictional harbor statement.",
            public_subjects_or_topics=("Synthetic harbor access",),
            source_selection=planner.case_binding(case_id).scope.source_refs,
            reviewer_role="Automated test fixture, not an analyst",
            expected_output="reviewed_brief",
        ),
    )
    confirmation = None
    if kind != "unbound_draft":
        draft = planner.bind(
            draft.brief_id,
            case_id,
            expected_head_revision_id=draft.revision_id,
            expected_case_fingerprint=service.case_fingerprint(case_id),
            author=AUTHOR,
        )
        confirmation = planner.confirm(
            draft.brief_id,
            expected_head_revision_id=draft.revision_id,
            expected_case_fingerprint=service.case_fingerprint(case_id),
            reviewer=REVIEWER,
        )
        assert planner.state_for_case(case_id).status == "confirmed"
    if kind == "imported_history":
        sender = service
        package = prepare_export(sender, "package", session_for_case(sender, case_id))
        service = workspace(path / "recipient")
        assert service.import_package(package.content) == case_id
        assert service.export_bundle(case_id) == sender.export_bundle(case_id)
        assert service.planning_briefs.history_for_case(case_id) == (
            sender.planning_briefs.history_for_case(case_id)
        )
    return service, case_id, draft, confirmation


def verify_scope_fixture(path, kind, purpose_id=GUIDE):
    service, case_id, draft, confirmation = make_scope_fixture(path, kind)
    state = session_for_case(service, case_id)
    before = table_snapshot(service.repository.db_path)
    captured = service.capture_case_package(case_id)
    view, handoff, _ = load_handoff_context(service, state, purpose_id=purpose_id)
    ui = render_handoff(handoff, view, state)
    text = ui_text(ui)
    dossier = prepare_dossier(
        service, state, purpose_id=purpose_id, expected_context=handoff["download_context"]
    ).content
    package = prepare_export(
        service, "package", state, expected_context=handoff["download_context"]
    )
    parsed = read_case_package(package.content, path / "not-installed", install_artifacts=False)
    assert parsed.bundle == captured.bundle
    assert parsed.planning_history == captured.planning_history
    assert service.capture_case_package(case_id) == captured
    assert table_snapshot(service.repository.db_path) == before

    for output in (text, dossier):
        assert "Session-only output guide purpose:" in output
        assert "Native collection source selection" in output
        assert "Not recorded in native evidence." in output
        assert "Saved planning source selections and attributed confirmations" in output
        assert "Saved source selection:" not in output
        assert "Confirm the investigation purpose and scope" not in output
    if purpose_id:
        assert NOTE in text
        gap = next(gap for gap in handoff["gaps"] if gap["kind"] == "unconfirmed_purpose")
        assert gap["label"] == "Review the session-only output guide"
        assert gap["action"] == "check_scope"
        assert handoff["references"]
        assert all(reference["url"] in dossier for reference in handoff["references"])
    else:
        assert "No output guide selected" in text and "No output guide selected" in dossier
        assert not any(gap["kind"] == "unconfirmed_purpose" for gap in handoff["gaps"])
    assert NOTE in dossier
    assert handoff["purpose"]["confirmed"] is False
    assert handoff["purpose"]["persisted"] is False
    assert handoff["readiness"]["policy_status"] == "unknown"
    assert handoff["readiness"]["submission_status"] == "not_submitted"
    assert (
        "- [ ] Confirm the purpose, recipient, reporting route, and investigation scope." in dossier
    )
    assert (
        "- [ ] Review platform eligibility and requirements independently of evidence export."
        in dossier
    )
    assert (
        "No approval, acceptance, or submission was recorded"
        in prepare_dossier(service, state, purpose_id=purpose_id).message
    )

    planning = handoff["planning"]
    assert planning["current_local_state"] == "not_projected"
    if kind == "unbound_draft":
        assert draft.binding is None
        assert service.planning_briefs.state_for_case(case_id) is None
        assert captured.planning_history is None
        assert planning["status"] == "not_recorded"
        assert planning["revisions"] == planning["confirmations"] == []
        assert draft.revision_id not in dossier
        assert service.planning_briefs.load(draft.brief_id).revision == draft
    else:
        assert planning["status"] == "history_available"
        assert len(planning["revisions"]) == 2
        assert planning["counts"]["unbound_ancestors"] == 1
        assert [row["confirmation_id"] for row in planning["confirmations"]] == [
            confirmation.confirmation_id
        ]
        assert all(row["grants_local_approval"] is False for row in planning["confirmations"])
        for output in (text, dossier):
            assert REVIEWER in output and draft.revision_id in output
            assert "local approval" in output.lower()
        local = service.planning_briefs.state_for_case(case_id)
        if kind == "local_confirmation":
            assert local.status == "confirmed" and local.confirmation_is_local is True
        else:
            assert local is None
            imported = service.planning_briefs.load(draft.brief_id, draft.revision_id)
            assert imported.imported is True and imported.confirmation_is_local is False
            assert imported.status == "needs_review"
            assert imported.confirmation.reviewer == REVIEWER
    assert table_snapshot(service.repository.db_path) == before
    return {
        "kind": kind,
        "synthetic_fixture_only": True,
        "human_validation": False,
        "dossier": dossier,
        "ui_text": text,
        "ui_components": json.loads(json.dumps(ui, cls=PlotlyJSONEncoder)),
        "planning": planning,
        "tables_before": before,
        "tables_after": table_snapshot(service.repository.db_path),
        "native_bundle": captured.bundle.model_dump(mode="json"),
        "planning_history": captured.planning_history.model_dump(mode="json")
        if captured.planning_history is not None
        else None,
        "package_sha256": hashlib.sha256(package.content).hexdigest(),
        "local_state": service.planning_briefs.state_for_case(case_id).status
        if service.planning_briefs.state_for_case(case_id)
        else None,
    }


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("purpose_id", [None, GUIDE])
def test_output_guide_scope_labels_preserve_evidence_and_planning_authority(
    tmp_path, kind, purpose_id
):
    verify_scope_fixture(tmp_path, kind, purpose_id)
