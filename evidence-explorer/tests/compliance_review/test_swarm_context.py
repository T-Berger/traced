from copy import deepcopy
from hashlib import sha256

import pytest

from evidence_explorer.alerts.repository import CaseData
from evidence_explorer.alerts.settings import AlertSettings
from evidence_explorer.compliance_review.alerts_bridge import project_alert_snapshot
from evidence_explorer.compliance_review.alerts_ui import commit_alert_proposals
from evidence_explorer.compliance_review.category_ui import category_review_panel, swarm_run_options
from evidence_explorer.compliance_review.review_ui import render_category_review
from evidence_explorer.compliance_review.swarm_context import project_category_swarm_context
from evidence_explorer.config import Settings
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.integrations.snapshots import workspace_snapshot
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.navigation import navigation_context
from evidence_explorer.review.workflow import evidence_fingerprint


def example():
    text = "Fictional saved comment."
    digest = sha256(text.encode()).hexdigest()
    bundle = EvidenceBundle.model_validate(
        {
            "case": {"id": "case", "title": "Fiction", "question": "Review?", "synthetic": True},
            "records": [
                {
                    "id": rid,
                    "kind": "telegram_message",
                    "source_label": rid,
                    "text": text,
                    "content_hash": digest,
                    "published_at": "2026-01-01T00:00:00Z",
                }
                for rid in ("one", "two")
            ],
            "fragments": [
                {
                    "id": f"f-{rid}",
                    "record_id": rid,
                    "selector": {"kind": "text", "start": 0, "end": 9},
                    "quote": "Fictional",
                }
                for rid in ("one", "two")
            ],
            "runs": [
                {
                    "id": "categories",
                    "method": "category",
                    "method_version": "1",
                    "input_fingerprint": "input",
                    "input_manifest": ["one"],
                    "created_at": "2026",
                },
                {
                    "id": "swarm",
                    "method": "swarm_detector.detector.analyze",
                    "method_version": "1",
                    "input_fingerprint": "swarm-input",
                    "input_manifest": ["one", "two"],
                    "created_at": "2026",
                    "full_inputs_available": False,
                    "coverage": {"note": "bounded fictional input"},
                },
            ],
            "findings": [
                {
                    "snapshot_id": "category",
                    "lineage_id": "category",
                    "version": 1,
                    "run_id": "categories",
                    "kind": "inference",
                    "statement": "Proposed category",
                    "details": {
                        "domain": "compliance_category",
                        "category": "racism",
                        "record_id": "one",
                        "record_content_hash": digest,
                        "target": "example target",
                        "origin": "model",
                        "model": "fixture",
                    },
                },
                *[
                    {
                        "snapshot_id": fid,
                        "lineage_id": fid,
                        "version": 1,
                        "run_id": "swarm",
                        "kind": "observation",
                        "statement": fid,
                        "details": {"domain": "swarm"},
                    }
                    for fid in ("swarm-one", "swarm-two", "swarm-unlinked")
                ],
            ],
            "links": [
                {
                    "id": "category-link",
                    "source": {"type": "fragment", "id": "f-one"},
                    "target": {"type": "finding", "id": "category"},
                    "relation": "supports",
                    "basis": "exact quotation",
                    "run_id": "categories",
                },
                *[
                    {
                        "id": f"link-{rid}",
                        "source": {"type": "fragment", "id": f"f-{rid}"},
                        "target": {"type": "finding", "id": f"swarm-{rid}"},
                        "relation": "supports",
                        "basis": "supplied timing observation",
                        "run_id": "swarm",
                    }
                    for rid in ("one", "two")
                ],
            ],
        }
    )
    data = bundle.model_dump(mode="json")
    view = {
        "bundle": data,
        "evidence_fingerprint": evidence_fingerprint(data),
        "case_fingerprint": "case-hash",
        "scenario": {"id": "assessment", "version": 1, "base_run_id": "categories"},
        "impact": {"excluded_record_ids": []},
        "events": [],
    }
    return view, bundle


def project(view, bundle, runs):
    return project_category_swarm_context(
        view, bundle, record_kinds=["telegram_message"], target="example target", swarm_run_ids=runs
    )


def test_swarm_context_requires_identical_record_and_preserves_run_and_limits():
    view, bundle = example()
    result = project(view, bundle, ["swarm"])
    row = result["rows"][0]
    assert row["record_id"] == "one"
    assert row["disposition"] is None
    assert [finding["finding_id"] for finding in row["swarm_context"]] == ["swarm-one"]
    finding = row["swarm_context"][0]
    assert finding["run_id"] == "swarm"
    assert finding["input_fingerprint"] == "swarm-input"
    assert finding["producer_reports_full_inputs"] is False
    assert finding["evidence_links"][0]["fragments"][0]["quote"] == "Fictional"
    assert [f["finding_id"] for f in result["unlinked_swarm_findings"]] == ["swarm-unlinked"]
    assert result["months"][0]["coverage"] == "unknown"


def test_selected_run_is_explicit_and_render_keeps_unlinked_findings_visible():
    view, bundle = example()
    assert swarm_run_options(bundle) == [
        {"label": "swarm_detector.detector.analyze · swarm", "value": "swarm"}
    ]
    assert "Saved Swarm runs (optional)" in str(category_review_panel())
    rendered = str(render_category_review(project(view, bundle, ["swarm"]), "context-token"))
    assert "Linked Swarm observations (1)" in rendered
    assert "swarm-one" in rendered
    assert "Complete inputs not established" in rendered
    assert "Category decisions do not carry over" in rendered
    assert "Record origin not specified" in rendered
    assert "swarm-unlinked" in rendered
    assert "swarm-two" not in rendered


def test_swarm_requires_explicit_run_and_matching_case_snapshot():
    view, bundle = example()
    assert project(view, bundle, [])["rows"][0]["swarm_context"] == []
    with pytest.raises(ValueError, match="Swarm detector runs"):
        project(view, bundle, ["categories"])
    with pytest.raises(ValueError, match="distinct"):
        project(view, bundle, ["swarm", "swarm"])
    stale = deepcopy(view)
    stale["evidence_fingerprint"] = "stale"
    with pytest.raises(ValueError, match="snapshot differ"):
        project(stale, bundle, ["swarm"])


def test_saved_review_export_and_exclusion_keep_swarm_and_category_meanings_separate(tmp_path):
    _, bundle = example()
    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    service.import_bundle(bundle.model_dump(mode="json"))
    scenario = service.start_scenario("case", "category")
    service.review("case", "category", scenario["id"], "qualify", "Fictional test review")

    with workspace_snapshot(service) as snapshot:
        view = snapshot.view("case", scenario["id"])
        captured = snapshot.export_bundle("case")
        result = project(view, captured, ["swarm"])
    row = result["rows"][0]
    assert row["disposition"] == "qualify"
    assert row["swarm_context"][0]["baseline_reviews"] == []
    assert row["swarm_context"][0]["finding_id"] == "swarm-one"
    assert EvidenceBundle.model_validate_json(captured.model_dump_json()).fingerprint() == (
        captured.fingerprint()
    )

    service.exclude(scenario["id"], "one", True, "Test this exclusion", 1)
    with workspace_snapshot(service) as snapshot:
        view = snapshot.view("case", scenario["id"])
        captured = snapshot.export_bundle("case")
        excluded = project(view, captured, ["swarm"])
    assert excluded["rows"][0]["excluded"] is True
    assert excluded["rows"][0]["disposition"] is None
    assert excluded["months"] == []
    assert excluded["rows"][0]["swarm_context"][0]["finding_id"] == "swarm-one"


def test_fictional_alert_swarm_review_and_package_reopen(tmp_path):
    """A declared exact mapping connects saved records, never detector approvals."""
    _, input_bundle = example()
    payload = input_bundle.model_dump(mode="json")
    payload["records"][0]["source_url"] = "https://example.com/post/1"
    bundle = EvidenceBundle.model_validate(payload)
    record = bundle.records[0]
    raw_capture = '{"fictional":true}'
    capture_hash = sha256(raw_capture.encode()).hexdigest()
    alerts = CaseData(
        case={"id": "fictional-analytics-case"},
        total_posts=1,
        labels=[
            {
                "id": "item",
                "case_id": "fictional-analytics-case",
                "status": "classified",
                "text": record.text,
                "url": record.source_url,
                "capture_sha256": capture_hash,
                "model": "fictional-classifier",
                "prompt_version": "fixture-v1",
                "disinformation": 0.9,
                "hate_speech": 0.1,
                "racism": 0.1,
                "harassment": 0.1,
                "reference_ids": [],
            }
        ],
        references={},
        last_run=None,
        provenance={"item": {"raw": raw_capture, "run": None}},
    )
    settings = Settings(data_dir=tmp_path / "source")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, settings.data_dir / "artifacts")
    service.import_bundle(bundle.model_dump(mode="json"))
    swarm_assessment = repository.create_scenario_state("case", "swarm", assessment_kind="scenario")
    service.review("case", "swarm-one", swarm_assessment.id, "qualify", "Prior behavioral review")
    state = session_for_case(service, "case")
    state.update(
        scenario_id=swarm_assessment.id,
        scenario_version=swarm_assessment.version,
        finding_id="swarm-one",
        record_id="one",
    )
    with workspace_snapshot(service) as snapshot:
        selected_view = snapshot.view("case", swarm_assessment.id)
        navigation = navigation_context(selected_view, state)
        saved = snapshot.export_bundle("case")
        fingerprint = snapshot.case_fingerprint("case")
        proposals = project_alert_snapshot(alerts, saved, {"item": "one"}, AlertSettings())
    assessment = commit_alert_proposals(
        service, state, proposals, fingerprint, navigation, "example target"
    )
    result_bundle = repository.get_bundle("case")
    category_finding = next(
        finding for finding in result_bundle.findings if finding.run_id == assessment.base_run_id
    )
    assert not any(
        review.run_id == assessment.base_run_id for review in service.export_bundle("case").reviews
    )
    service.review("case", category_finding.snapshot_id, assessment.id, "use", "Reviewed source")
    with workspace_snapshot(service) as snapshot:
        view = snapshot.view("case", assessment.id)
        saved = snapshot.export_bundle("case")
        result = project_category_swarm_context(
            view,
            saved,
            record_kinds=["telegram_message"],
            target="example target",
            swarm_run_ids=["swarm"],
        )
    package = service.export_package("case")
    row = result["rows"][0]
    assert row["disposition"] == "use"
    assert row["fragments"][0]["record_id"] == "one"
    assert row["swarm_context"][0]["finding_id"] == "swarm-one"
    assert row["swarm_context"][0]["baseline_reviews"] == []
    assert result["months"][0]["coverage"] == "unknown"
    assert not any("raw" in finding.details for finding in saved.findings)

    recipient_settings = Settings(data_dir=tmp_path / "recipient")
    recipient_repo = WorkspaceRepository(recipient_settings.db_path)
    recipient_repo.initialize()
    recipient = WorkspaceService(
        recipient_repo, recipient_settings, recipient_settings.data_dir / "artifacts"
    )
    recipient.import_package(package)
    reopened = recipient.export_bundle("case")
    assert (
        next(item for item in reopened.records if item.id == "one").content_hash
        == record.content_hash
    )
    assert any(review.finding_snapshot_id == "swarm-one" for review in reopened.reviews)
    assert any(
        review.finding_snapshot_id == category_finding.snapshot_id for review in reopened.reviews
    )
    assert all(review.finding_snapshot_id != "swarm-two" for review in reopened.reviews)


def test_dossier_binds_explicit_category_and_swarm_selection_to_saved_snapshot(tmp_path):
    from evidence_explorer.review.integrations.handoff_service import (
        load_handoff_context,
        prepare_dossier,
    )

    _, bundle = example()
    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    service.import_bundle(bundle.model_dump(mode="json"))
    scenario = service.start_scenario("case", "category")
    service.review("case", "category", scenario["id"], "qualify", "Reviewed example")
    state = session_for_case(service, "case")
    state.update(
        scenario_id=scenario["id"],
        scenario_version=scenario["version"],
        finding_id="category",
        record_id="one",
    )
    selection = {
        "target": "example target",
        "record_kinds": ["telegram_message"],
        "swarm_run_ids": ["swarm"],
    }
    _, handoff, _ = load_handoff_context(service, state, category_selection=selection)
    dossier = prepare_dossier(
        service, state, category_selection=selection, expected_context=handoff["download_context"]
    ).content
    assert "Reviewed example" in dossier
    assert "swarm\\-one" in dossier
    assert "unreviewed here; not a category decision" in dossier
    assert "swarm\\-unlinked" in dossier
    assert "statement omitted; no linked saved record" in dossier
    assert "sample coverage is unknown" in dossier
    different = {**selection, "swarm_run_ids": []}
    with pytest.raises(ValueError, match="handoff changed"):
        prepare_dossier(
            service,
            state,
            category_selection=different,
            expected_context=handoff["download_context"],
        )


def test_category_dossier_redacts_unapproved_rows_and_escapes_untrusted_text():
    from evidence_explorer.compliance_review.dossier import (
        redact_category_brief,
        render_category_dossier,
    )

    view, bundle = example()
    projection = project(view, bundle, ["swarm"])
    row = projection["rows"][0]
    row["fragments"][0]["quote"] = "WITHHELD_SECRET ![x](https://bad.example/x) <script>"
    row["swarm_context"][0]["statement"] = "UNREVIEWED_SWARM_SECRET"
    result = render_category_dossier(projection)
    assert "WITHHELD_SECRET" not in result
    assert r"WITHHELD\_SECRET" not in result
    assert "UNREVIEWED_SWARM_SECRET" not in result
    assert r"UNREVIEWED\_SWARM\_SECRET" not in result
    handoff = {
        "category_review": projection,
        "brief": {
            "included": [],
            "omitted": [{"finding_id": row["finding_id"], "statement": "WITHHELD_SECRET"}],
            "case": {"title": "Example", "question": ""},
            "assessment": {"label": "Example", "base_run_id": "run"},
            "methods": [],
            "coverage": {},
            "limitations": [],
            "excluded_record_ids": [],
        },
        "scope": {"collection_scope": None},
        "gaps": [],
        "planning": {},
    }
    assert "WITHHELD_SECRET" not in str(redact_category_brief(handoff)["brief"])
    row["disposition"] = "use"
    row["decision"] = {"disposition": "use", "reviewer": "Example", "reason": "OK"}
    result = render_category_dossier(projection)
    assert r"WITHHELD\_SECRET" in result
    assert "![x](https://bad.example/x)" not in result
    assert "<script>" not in result
    assert "producer reports full inputs: False" in result


def test_dossier_requires_separate_explicit_swarm_opt_in():
    from evidence_explorer.review.integrations.handoff_ui import _category_selection

    args = (["include"], "example target", ["telegram_message"], ["swarm"])
    assert _category_selection(*args)["swarm_run_ids"] == []
    assert _category_selection(*args, ["include"])["swarm_run_ids"] == ["swarm"]
    with pytest.raises(ValueError, match="Select at least one Swarm run"):
        _category_selection(["include"], "example target", ["telegram_message"], [], ["include"])
    with pytest.raises(ValueError, match="Include the selected category"):
        _category_selection([], "example target", ["telegram_message"], ["swarm"], ["include"])


def test_complete_dossier_escapes_untrusted_category_and_base_case_content(tmp_path):
    from evidence_explorer.review.integrations.handoff_service import prepare_dossier

    _, original = example()
    data = original.model_dump(mode="json")
    attack = "Fictional ![badge](https://bad.example/pixel) <img src=x>"
    data["case"]["title"] = attack
    data["records"][0]["text"] = attack
    data["records"][0]["content_hash"] = sha256(attack.encode()).hexdigest()
    data["fragments"][0]["quote"] = attack
    data["fragments"][0]["selector"]["end"] = len(attack)
    data["findings"][0]["details"]["record_content_hash"] = sha256(attack.encode()).hexdigest()
    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    service.import_bundle(data)
    scenario = service.start_scenario("case", "category")
    service.review("case", "category", scenario["id"], "use", "Reviewed")
    state = session_for_case(service, "case")
    state.update(
        scenario_id=scenario["id"],
        scenario_version=scenario["version"],
        finding_id="category",
        record_id="one",
    )
    selection = {
        "target": "example target",
        "record_kinds": ["telegram_message"],
        "swarm_run_ids": ["swarm"],
    }
    output = prepare_dossier(service, state, category_selection=selection).content
    assert "![badge](https://bad.example/pixel)" not in output
    assert "<img src=x>" not in output
    assert r"\!\[badge\]\(https://bad\.example/pixel\)" in output


def test_category_dossier_hides_other_targets_withheld_and_unreviewed_statements(tmp_path):
    from evidence_explorer.review.integrations.handoff_service import prepare_dossier

    _, bundle = example()
    data = bundle.model_dump(mode="json")
    for finding_id, statement in (
        ("off-target-withheld", "SECRET OFFTARGET WITHHELD"),
        ("off-target-unreviewed", "SECRET OFFTARGET UNREVIEWED"),
    ):
        finding = deepcopy(data["findings"][0])
        finding.update(snapshot_id=finding_id, lineage_id=finding_id, statement=statement)
        finding["details"]["target"] = "other target"
        data["findings"].append(finding)
    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    service.import_bundle(data)
    scenario = service.start_scenario("case", "category")
    service.review("case", "category", scenario["id"], "use", "Reviewed selected target")
    service.review(
        "case", "off-target-withheld", scenario["id"], "withhold", "SECRET OFFTARGET REASON"
    )
    state = session_for_case(service, "case")
    state.update(
        scenario_id=scenario["id"],
        scenario_version=scenario["version"],
        finding_id="category",
        record_id="one",
    )
    selection = {
        "target": "example target",
        "record_kinds": ["telegram_message"],
        "swarm_run_ids": [],
    }
    output = prepare_dossier(service, state, category_selection=selection).content
    assert "Reviewed selected target" in output
    assert "SECRET OFFTARGET" not in output
    assert "off\\-target\\-withheld" not in output
    assert "off\\-target\\-unreviewed" not in output
