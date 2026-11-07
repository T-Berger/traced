"""Claim checking enqueues durable work and keeps proposal/reviewer state separate."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from dash import Dash, html, no_update
from dash.development.base_component import Component

from evidence_explorer.claim_check import ui
from evidence_explorer.claim_check.contracts import ClaimCheckResult, ProposedAssessment
from evidence_explorer.claim_check.service import ClaimCheckService
from evidence_explorer.config import Settings
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService


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


@pytest.fixture
def desk(tmp_path):
    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    bundle = load_demo_bundle()
    service.import_bundle(bundle.model_dump(mode="json"))
    checks = ClaimCheckService(repository, settings)
    app = Dash(__name__, suppress_callback_exceptions=True)
    app.layout = lambda: html.Div([ui.claim_check_panel()])
    ui.register_claim_check_callbacks(app, service, checks)

    def callback(output):
        return next(
            item["callback"].__wrapped__ for key, item in app.callback_map.items() if output in key
        )

    state = {"case_id": bundle.case.id, "finding_id": bundle.findings[0].snapshot_id}
    return service, checks, app, callback, state


def test_submit_only_queues_work_and_restored_ui_reads_saved_jobs(desk, monkeypatch):
    service, checks, _, callback, state = desk
    fingerprint = service.case_fingerprint(state["case_id"])
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id="cc-submit"))
    operation, message = callback("cc-operation.data")(
        1, 0, state, "Harbor original records", "https://example.org/article", 3, None
    )
    assert "Queued" in text(message)
    job = checks.get(operation["job_id"])
    assert job.status == "queued" and job.snapshot is None and job.result is None
    assert job.payload.policy.queries == ["Harbor original records"]
    assert job.payload.policy.seed_urls == ["https://example.org/article"]
    assert service.case_fingerprint(state["case_id"]) == fingerprint
    assert len(service.list_cases()) == 1
    # A fresh service reads the job; the browser need not retain evidence or a result.
    reopened = ClaimCheckService(service.repository, service.settings)
    assert reopened.get(job.id).payload == job.payload
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id="rv-session"))
    options, selected, timer_disabled, submit_disabled = callback("cc-run.options")(
        state, 0, None, None
    )
    assert options[0]["value"] == selected == job.id
    assert timer_disabled is False and submit_disabled is True
    panel, open_disabled, retry_disabled = callback("cc-progress.children")(selected, 0, state)
    assert "Queued" in text(panel)
    assert open_disabled is retry_disabled is True


def test_invalid_limits_and_cross_finding_retry_do_not_enqueue(desk, monkeypatch):
    _, checks, _, callback, state = desk
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id="cc-submit"))
    submit = callback("cc-operation.data")
    result, message = submit(1, 0, state, "a\nb\nc\nd", "", 6, None)
    assert result is no_update and "3" in text(message)
    assert checks.list(state["case_id"]) == []
    operation, _ = submit(2, 0, state, "", "", 6, None)
    other = dict(state, finding_id="harbor:c3-v1")
    if other["finding_id"] == state["finding_id"]:
        other["finding_id"] = "different-finding"
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id="cc-retry"))
    result, message = submit(2, 1, other, "", "", 6, operation["job_id"])
    assert result is no_update and "this finding" in text(message)
    assert len(checks.list(state["case_id"])) == 1
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id="rv-session"))
    options, selected, disabled, _ = callback("cc-run.options")(
        other, 0, operation, operation["job_id"]
    )
    assert options == [] and selected is None and disabled is True


def test_completed_result_can_open_and_stale_failure_cannot_retry(desk, monkeypatch):
    service, checks, _, callback, state = desk
    job = checks.submit(state["case_id"], state["finding_id"])
    completed = job.model_copy(
        update={
            "status": "completed",
            "stage": "awaiting_review",
            "result": ClaimCheckResult(
                result_case_id=state["case_id"],
                result_finding_id=state["finding_id"],
                assessment=ProposedAssessment(
                    status="insufficient_evidence", explanation="The original document is missing."
                ),
                evidence_fingerprint="snapshot-fingerprint",
                coverage="empty",
                sources_count=0,
            ),
        }
    )
    monkeypatch.setattr(checks, "get", lambda _: completed)
    panel, open_disabled, retry_disabled = callback("cc-progress.children")(job.id, 1, state)
    assert "Model proposal: Insufficient evidence" in text(panel)
    assert open_disabled is False and retry_disabled is True
    options, case_id, next_state, message = callback("rv-case.options")(1, job.id, state)
    assert case_id == state["case_id"] == next_state["case_id"]
    assert next_state["navigate_to"] == "review"
    assert options and "record your decision" in text(message)
    assert service.repository.list_events(case_id) == []
    stale = job.model_copy(update={"status": "failed", "stale": True, "error": "No sources"})
    monkeypatch.setattr(checks, "get", lambda _: stale)
    panel, open_disabled, retry_disabled = callback("cc-progress.children")(job.id, 2, state)
    assert "fresh check" in text(panel)
    assert open_disabled is retry_disabled is True


def test_portable_assessment_has_inspectable_citations_without_a_live_job():
    details = {
        "domain": "claim_check",
        "synthetic": True,
        "assessment": {
            "status": "contradicted",
            "explanation": "The cited original gives a different date.",
            "citations": [
                {"fragment_id": "fragment-1", "quote": "Published in 2020", "stance": "contradicts"}
            ],
            "unresolved_questions": ["Who changed the caption?"],
            "model": "local-test-model",
            "metadata": {
                "challenge": {
                    "status": "completed",
                    "scope": "Same saved passages; not independent evidence.",
                    "findings": [
                        {
                            "finding_type": "contradiction",
                            "explanation": "The recording predates the claimed event.",
                            "fragment_ids": ["fragment-1"],
                            "quotes": ["Published in 2020"],
                        }
                    ],
                }
            },
        },
        "claim_check": {
            "collection_coverage": "partial",
            "source_synthetic": True,
            "source_synthetic_basis": "finding_metadata",
            "source_case_synthetic": False,
            "origin_limitations": ["The original claim has no publication date."],
            "issues": [{"message": "One original page was unavailable."}],
            "search_batches": [{"query": "original video date", "provider": "test", "hits": []}],
        },
    }
    panel = ui.assessment_panel(
        details,
        [{"id": "fragment-1", "record_id": "record-1"}],
        ["The external search was bounded."],
    )
    content = text(panel)
    assert "Model proposal: Contradicted by saved evidence" in content
    assert "briefing decision is recorded separately" in content
    assert "Published in 2020" in content and "One original page was unavailable" in content
    assert "original video date" in content and "partial" in content
    assert "The recording predates the claimed event" in content
    assert "not independent evidence" in content
    assert "The external search was bounded" in content
    assert "The original claim has no publication date" in content
    assert "Claim origin: Synthetic test claim" in content
    assert "Originating case: Not marked synthetic" in content
    buttons = [node for node in components(panel) if isinstance(node, html.Button)]
    assert buttons[0].id == {"type": "rv-record", "rid": "record-1", "location": "cc-0"}
    assert buttons[1].id == {
        "type": "rv-record",
        "rid": "record-1",
        "location": "cc-objection-0-0",
    }
    details["assessment"]["metadata"]["challenge"]["findings"] = []
    assert "This does not verify the claim" in text(ui.assessment_panel(details))


@pytest.mark.parametrize(
    "provenance,finding_origin,claim_label,case_label,legacy",
    [
        (
            {
                "source_synthetic": True,
                "source_synthetic_basis": "finding_metadata",
                "source_case_synthetic": False,
            },
            {"synthetic": True},
            "Claim origin: Synthetic test claim.",
            "Not marked synthetic.",
            False,
        ),
        (
            {
                "source_synthetic": False,
                "source_synthetic_basis": "finding_metadata",
                "source_case_synthetic": True,
            },
            {"synthetic": False},
            "Claim origin: Declared non-synthetic claim; factual accuracy requires review.",
            "Synthetic example.",
            False,
        ),
        (
            {
                "source_synthetic": None,
                "source_synthetic_basis": "unspecified",
                "source_case_synthetic": True,
            },
            {},
            "Claim origin not specified.",
            "Synthetic example.",
            False,
        ),
        (
            {
                "source_synthetic": True,
                "source_synthetic_basis": "unspecified",
                "source_case_synthetic": False,
            },
            {},
            "Claim origin not specified.",
            "Not marked synthetic.",
            False,
        ),
        (
            {"source_synthetic": True},
            {},
            "Claim origin not specified.",
            "Synthetic example.",
            True,
        ),
        (
            {"source_synthetic": False},
            {},
            "Claim origin not specified.",
            "Not marked synthetic.",
            True,
        ),
        (
            {
                "source_synthetic": "false",
                "source_synthetic_basis": "finding_metadata",
                "source_case_synthetic": "true",
            },
            {"synthetic": "false"},
            "Claim origin not specified.",
            "Not specified.",
            False,
        ),
        ({}, {}, "Claim origin not specified.", "Not specified.", False),
        (
            {
                "source_synthetic": True,
                "source_synthetic_basis": "finding_metadata",
                "source_case_synthetic": False,
            },
            {"synthetic": False},
            "Claim origin: Declared non-synthetic claim; factual accuracy requires review.",
            "Not marked synthetic.",
            False,
        ),
        (
            {
                "source_synthetic": True,
                "source_synthetic_basis": "finding_metadata",
                "source_case_synthetic": False,
            },
            {},
            "Claim origin not specified.",
            "Not marked synthetic.",
            False,
        ),
    ],
)
def test_portable_claim_origin_is_visible_and_separate_from_case_origin_without_mutation(
    provenance, finding_origin, claim_label, case_label, legacy
):
    details = {
        "domain": "claim_check",
        "assessment": {"status": "insufficient_evidence", "explanation": "Review saved sources."},
        "claim_check": provenance,
        **finding_origin,
    }
    before = deepcopy(details)
    panel = ui.assessment_panel(details)
    # Origin is visible alongside the saved assessment, not hidden inside a disclosure.
    paragraphs = [
        text(child)
        for child in panel.children
        if isinstance(child, html.P) and text(child).startswith("Claim origin")
    ]
    assert len(paragraphs) == 1
    content = paragraphs[0]
    assert claim_label in content
    assert f"Originating case: {case_label}" in content
    assert "Saved records retain their own provenance." in content
    assert ("older result's source flag labels only the originating case" in content) is legacy
    if claim_label == "Claim origin not specified.":
        assert "Synthetic test claim" not in content
        assert "Declared non-synthetic claim" not in content
    assert details == before


@pytest.mark.parametrize("pinned_origin", [True, False])
@pytest.mark.parametrize(
    "finding_origin,expected_override",
    [
        ({}, None),
        ({"synthetic": None}, "Claim origin not specified."),
        ({"synthetic": "true"}, "Claim origin not specified."),
        ({"synthetic": True}, "Claim origin: Synthetic test claim."),
        (
            {"synthetic": False},
            "Claim origin: Declared non-synthetic claim; factual accuracy requires review.",
        ),
    ],
)
def test_legacy_pinned_finding_origin_is_used_only_when_current_marker_is_absent(
    pinned_origin, finding_origin, expected_override
):
    details = {
        "domain": "claim_check",
        "assessment": {"status": "insufficient_evidence", "explanation": "Review saved sources."},
        "claim_check": {
            "source_synthetic": not pinned_origin,
            "source_finding_snapshot": {
                "snapshot_id": "original-finding",
                "details": {"synthetic": pinned_origin},
            },
        },
        **finding_origin,
    }
    before = deepcopy(details)
    content = text(ui.assessment_panel(details))
    expected = expected_override or (
        "Claim origin: Synthetic test claim."
        if pinned_origin
        else "Claim origin: Declared non-synthetic claim; factual accuracy requires review."
    )
    assert expected in content
    assert (
        "Originating case: Not marked synthetic."
        if pinned_origin
        else "Originating case: Synthetic example."
    ) in content
    if expected == "Claim origin not specified.":
        assert "Synthetic test claim" not in content
        assert "Declared non-synthetic claim" not in content
    assert details == before


def test_integrated_layout_registers_claim_check_once_without_network(desk):
    from evidence_explorer.review.integrations.web import create_app

    service, _, _, _, _ = desk
    app = create_app(service)
    client = app.server.test_client()
    assert client.get("/_dash-layout").status_code == 200
    assert client.get("/_dash-dependencies").status_code == 200
    nodes = list(components(app.layout()))
    ids = [getattr(node, "id", None) for node in nodes]
    assert ids.count("cc-submit") == ids.count("cc-assessment") == ids.count("cc-tick") == 1
    assert all(
        callback["prevent_initial_call"]
        for callback in app._callback_list
        if "cc-operation.data" in callback["output"]
    )


def test_claim_check_keeps_case_label_separate_from_claim_and_record_origin(desk):
    from evidence_explorer.review import web
    from evidence_explorer.review.workspace_views import case_cards

    service, _, _, _, state = desk
    original = service.get_case(state["case_id"])
    payload = service.get_case(state["case_id"])
    payload["case"]["id"] = "claim-check:mixed-provenance"
    payload["case"]["limitations"] = ["External sources are a bounded selection."]
    payload["findings"][0]["details"] = {
        "domain": "claim_check",
        "synthetic": True,
        "assessment": {"status": "insufficient_evidence", "explanation": "Needs review."},
        "claim_check": {
            "source_synthetic": True,
            "source_synthetic_basis": "finding_metadata",
            "source_case_synthetic": True,
            "origin_limitations": ["The claim is a test fixture."],
        },
    }
    payload["records"][0]["metadata"]["synthetic"] = False
    service.import_bundle(payload)
    app = web.create_app(service)
    render = next(
        item["callback"].__wrapped__
        for key, item in app.callback_map.items()
        if "rv-synthetic.children" in key
    )
    mixed_state = dict(state, case_id=payload["case"]["id"])
    banner = text(render(mixed_state)[8])
    assert "Selected case: Synthetic example" in banner
    assert "Saved records retain their own provenance" in banner
    assert "Test claim" not in banner
    assert "Fictional evidence" not in banner
    generic_banner = text(render(state)[8])
    assert "Selected case: Synthetic example" in generic_banner
    assert "separately collected sources are not labeled fictional" in generic_banner
    assert "Fictional evidence" not in generic_banner
    assert service.get_case(mixed_state["case_id"])["records"][0]["metadata"]["synthetic"] is False
    assert service.get_case(state["case_id"]) == original
    cards = text(case_cards([{"case": payload["case"]}], None))
    assert "Synthetic example" in cards and "Test claim" not in cards
    assert "Synthetic example" in text(case_cards([{"case": original["case"]}], None))

    # Portable case metadata supplies limits even without the originating live job.
    isolated = Dash(__name__, suppress_callback_exceptions=True)
    isolated.layout = ui.claim_check_panel
    ui.register_claim_check_callbacks(isolated, service)
    projection = next(
        item["callback"].__wrapped__
        for key, item in isolated.callback_map.items()
        if "cc-assessment.children" in key
    )
    assessment, _ = projection(mixed_state)
    assert "Claim origin: Synthetic test claim" in text(assessment)
    assert "External sources are a bounded selection" in text(assessment)
    assert "The claim is a test fixture" in text(assessment)


@pytest.mark.parametrize("input_kind", ["citation", "challenge", "dependency"])
def test_exclusion_marks_saved_proposal_and_challenge_as_not_recomputed(desk, input_kind):
    service, _, _, callback, state = desk
    payload = service.get_case(state["case_id"])
    payload["case"]["id"] = f"claim-check:scenario-{input_kind}"
    finding = payload["findings"][0]
    fragment = payload["fragments"][0]
    record_id = fragment["record_id"]
    assessment = {
        "status": "supported",
        "explanation": "The saved input was considered in the original check.",
        "citations": [],
        "metadata": {"challenge": {"status": "completed", "findings": []}},
    }
    if input_kind == "citation":
        assessment["citations"] = [
            {"fragment_id": fragment["id"], "quote": fragment["quote"], "stance": "supports"}
        ]
    if input_kind == "challenge":
        assessment["metadata"]["challenge"]["findings"] = [
            {
                "finding_type": "contradiction",
                "explanation": "A candidate objection from the saved source.",
                "fragment_ids": [fragment["id"]],
                "quotes": [fragment["quote"]],
            }
        ]
    finding["details"] = {"domain": "claim_check", "assessment": assessment, "claim_check": {}}
    payload["links"] = [
        link for link in payload["links"] if link["target"]["id"] != finding["snapshot_id"]
    ]
    if input_kind == "dependency":
        payload["links"].append(
            {
                "id": "assessment-input",
                "source": {"type": "record", "id": record_id},
                "target": {"type": "finding", "id": finding["snapshot_id"]},
                "relation": "depends_on",
                "basis": "Saved assessment input; not a factual support claim.",
            }
        )
    service.import_bundle(payload)
    selected = {"case_id": payload["case"]["id"], "finding_id": finding["snapshot_id"]}
    project = callback("cc-assessment.children")
    panel, _ = project(selected)
    assert "Proposed evidence assessment" in text(panel)
    assert "not been recomputed" not in text(panel)

    scenario = service.start_scenario(selected["case_id"])
    unrelated_id = next(record["id"] for record in payload["records"] if record["id"] != record_id)
    changed = service.exclude(
        scenario["id"], unrelated_id, True, "Unrelated input", scenario["version"]
    )
    scenario_state = dict(selected, scenario_id=scenario["id"])
    panel, search_note = project(scenario_state)
    assert "not been recomputed" not in text(panel)
    assert "exclusions are not applied to the new search" in text(search_note)

    changed = service.exclude(
        scenario["id"], record_id, True, "Check without source", changed["version"]
    )
    events = service.repository.list_events(selected["case_id"])
    panel, _ = project(scenario_state)
    rendered = text(panel)
    assert "Saved evidence assessment · review needed" in rendered
    assert "Model proposal: Supported by saved evidence · original result" in rendered
    assert "not been recomputed" in rendered
    assert "Saved automated challenge · not recomputed" in rendered
    if input_kind in {"citation", "challenge"}:
        assert fragment["quote"] in rendered
        assert "Excluded from this scenario · saved quotation" in rendered
    assert service.repository.list_events(selected["case_id"]) == events

    service.exclude(scenario["id"], record_id, False, "Restore source", changed["version"])
    restored, _ = project(scenario_state)
    assert "not been recomputed" not in text(restored)
    baseline, _ = project(selected)
    assert "Proposed evidence assessment" in text(baseline)
    assert (
        service.get_case(selected["case_id"])["findings"][0]["details"]["assessment"] == assessment
    )
