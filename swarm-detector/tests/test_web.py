import base64

import pytest

from swarm_detector.service import Workbench


@pytest.fixture
def wb(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("SWARM_BASIC_AUTH_PASSWORD", raising=False)
    workbench = Workbench(tmp_path / "web.db")
    workbench.ensure_sample()
    return workbench


def test_basic_auth_protects_application_and_callback_data(wb, monkeypatch):
    from swarm_detector.web import create_app

    monkeypatch.setenv("SWARM_BASIC_AUTH_USER", "demo-reviewer")
    monkeypatch.setenv("SWARM_BASIC_AUTH_PASSWORD", "test-only-password")
    app = create_app(wb)
    client = app.server.test_client()
    assert client.get("/").status_code == 401
    assert client.get("/_dash-layout").status_code == 401
    assert client.post("/_dash-update-component", json={}).status_code == 401
    response = client.get("/health")
    assert response.status_code == 200
    assert set(response.json) == {"status", "ready"}
    token = base64.b64encode(b"demo-reviewer:test-only-password").decode()
    assert (
        client.get("/_dash-layout", headers={"Authorization": f"Basic {token}"}).status_code == 200
    )


def test_partial_auth_configuration_fails_closed(wb, monkeypatch):
    from swarm_detector.web import create_app

    monkeypatch.setenv("SWARM_BASIC_AUTH_USER", "reviewer")
    with pytest.raises(RuntimeError, match="both"):
        create_app(wb)


def test_unready_health_is_not_success(wb, monkeypatch):
    from swarm_detector.web import create_app

    monkeypatch.setattr(wb, "health", lambda: {"ready": False, "status": "unavailable"})
    response = create_app(wb).server.test_client().get("/health")
    assert response.status_code == 503
    assert response.json == {"ready": False, "status": "unavailable"}


def test_browser_receives_case_scope_instead_of_entire_dataset(wb):
    from swarm_detector.web import create_app

    app = create_app(wb)
    case = wb.list_cases(kind="candidate")[0]
    loader = app.callback_map["detail-store.data"]["callback"].__wrapped__
    detail = loader(case["case_id"], {"run_id": wb.overview()["run_id"]})
    assert "messages" not in detail["dataset"]
    assert "posts" not in detail["dataset"]
    assert "pairs" not in detail["run"]
    assert "groups" not in detail["run"]
    assert len(detail["messages"]) < wb.overview()["message_count"]
    assert any(
        m["account_key"] not in set(case["account_keys"]) for m in detail["context_messages"]
    )


def test_importing_web_factory_has_no_database_or_startup_side_effects(tmp_path):
    import os
    import subprocess
    import sys

    database = tmp_path / "not-created.db"
    environment = {
        **os.environ,
        "SWARM_DB_PATH": str(database),
        # Even partial startup configuration is irrelevant until a factory is called.
        "SWARM_BASIC_AUTH_USER": "reviewer",
        "SWARM_BASIC_AUTH_PASSWORD": "",
    }
    subprocess.run(
        [sys.executable, "-c", "from swarm_detector.web import create_app"],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert not database.exists()


def test_explicit_wsgi_entry_point_starts_a_ready_application(tmp_path):
    import os
    import subprocess
    import sys

    database = tmp_path / "wsgi.db"
    environment = {
        **os.environ,
        "SWARM_DB_PATH": str(database),
        "SWARM_BASIC_AUTH_USER": "",
        "SWARM_BASIC_AUTH_PASSWORD": "",
    }
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from swarm_detector.wsgi import server; "
            "assert server.test_client().get('/health').status_code == 200",
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert database.exists()


def test_unexpected_callback_failures_are_logged_and_remain_visible(wb, monkeypatch, caplog):
    from swarm_detector.web import create_app

    app = create_app(wb)

    def fail(_case_id, *, run_id=None):
        raise RuntimeError("Storage could not read the selected case")

    monkeypatch.setattr(wb, "case_detail", fail)
    loader = app.callback_map["detail-store.data"]["callback"].__wrapped__
    result = loader("any-case", {"run_id": wb.overview()["run_id"]})

    assert result == {"error": "Storage could not read the selected case"}
    assert "Unable to load the selected case" in caplog.text
    assert "RuntimeError: Storage could not read the selected case" in caplog.text


def test_case_loading_stays_on_the_workspace_run_when_active_analysis_changes(wb):
    from swarm_detector.web import create_app

    app = create_app(wb)
    pinned = wb.overview()["run_id"]
    case = wb.list_cases(kind="candidate", run_id=pinned)[0]
    wb.run_analysis({"randomizations": 3})
    assert wb.overview()["run_id"] != pinned
    loader = app.callback_map["detail-store.data"]["callback"].__wrapped__
    detail = loader(case["case_id"], {"run_id": pinned})
    assert detail["run"]["run_id"] == pinned
    assert detail["case"]["case_id"] == case["case_id"]


def test_missing_pinned_run_never_falls_back_to_a_new_active_run(wb, monkeypatch):
    from swarm_detector.web import create_app

    app = create_app(wb)

    def forbidden(*args, **kwargs):
        raise AssertionError("An unpinned case must never read the active run")

    monkeypatch.setattr(wb, "case_detail", forbidden)
    loader = app.callback_map["detail-store.data"]["callback"].__wrapped__
    assert loader("case-from-an-old-screen", {"run_id": None}) == {}


def test_case_changes_preserve_a_complete_unsaved_draft_by_case_and_run(wb):
    from swarm_detector.review_state import dirty, open_review

    cases = wb.list_cases()
    detail_a, detail_b = (wb.case_detail(c["case_id"]) for c in cases[:2])
    state_a, values_a, drafts = open_review(detail_a, {})
    draft = {
        **values_a,
        "reviewer": "Analyst",
        "reason": "A public call may explain the activity.",
        "claim": "Observed claim",
        "endorsement": "quotes",
        "alignment": "mixed",
        "narrative_notes": "The comment reports the claim rather than endorsing it.",
        "evidence_refs": [detail_a["messages"][0]["message_key"]],
    }
    state_b, values_b, drafts = open_review(detail_b, drafts, state_a, draft)
    restored_state, restored, drafts = open_review(detail_a, drafts, state_b, values_b)
    assert restored == draft
    assert dirty(restored, restored_state)
    other_run = {**detail_a, "run": {**detail_a["run"], "run_id": "different-run"}}
    other_state, other_values, _ = open_review(other_run, drafts, restored_state, restored)
    assert other_state["key"] != restored_state["key"]
    assert other_values["reason"] == ""


@pytest.mark.parametrize(
    "field,change",
    [
        ("reviewer", "Changed reviewer"),
        ("decision", "Coordination supported"),
        ("reason", "Changed reason"),
        ("claim", "Changed claim"),
        ("endorsement", "rebuts"),
        ("alignment", "mixed"),
        ("narrative_notes", "Changed contextual reasoning"),
        ("evidence_refs", ["source-a"]),
    ],
)
def test_every_review_field_contributes_to_unsaved_changes(field, change):
    from swarm_detector.review_state import dirty, values_from_review

    baseline = values_from_review()
    assert not dirty(baseline, {"baseline": baseline})
    assert dirty({**baseline, field: change}, {"baseline": baseline})


def test_review_conflict_preserves_draft_instead_of_overwriting_another_reviewer(wb):
    from swarm_detector.review_state import open_review, preserve_draft, save_review

    detail = wb.case_detail(wb.list_cases()[0]["case_id"])
    state, values, drafts = open_review(detail, {})
    values = {
        **values,
        "reviewer": "First analyst",
        "reason": "Repeated episodes warrant further investigation.",
    }
    drafts = preserve_draft(drafts, state, values)
    wb.save_review(
        state["case_id"],
        "Second analyst",
        "Insufficient evidence",
        "A public explanation still needs to be investigated.",
        run_id=state["run_id"],
        expected_revision=0,
    )
    with pytest.raises(ValueError):
        save_review(wb, state, values, drafts)
    assert drafts[state["key"]]["values"] == values
    assert wb.get_review(state["case_id"], run_id=state["run_id"])["reviewer"] == "Second analyst"


def test_export_is_the_previewed_unreviewed_revision_even_after_another_save(wb):
    from swarm_detector.review_state import export_preview, open_review

    detail = wb.case_detail(wb.list_cases()[0]["case_id"])
    state, values, _ = open_review(detail, {})
    preview = wb.brief_preview(state["case_id"], run_id=state["run_id"], revision=0)
    preview["key"] = state["key"]
    wb.save_review(
        state["case_id"],
        "Later reviewer",
        "Coordination supported",
        "Repeated behavioral evidence supports coordination.",
        run_id=state["run_id"],
        expected_revision=0,
    )
    assert export_preview(wb, preview, state, values, "markdown") == preview["content"]
    assert "Later reviewer" not in preview["content"]
    with pytest.raises(ValueError, match="Save your changes"):
        export_preview(
            wb, preview, state, {**values, "reason": "Unsaved changed reason"}, "markdown"
        )


def test_saving_a_new_revision_requires_a_new_preview_before_export(wb):
    from swarm_detector.review_state import export_preview, open_review, save_review

    detail = wb.case_detail(wb.list_cases()[0]["case_id"])
    state, values, drafts = open_review(detail, {})
    preview = wb.brief_preview(state["case_id"], run_id=state["run_id"], revision=0)
    preview["key"] = state["key"]
    state, values, _ = save_review(
        wb,
        state,
        {**values, "reviewer": "Reviewer", "reason": "Alternative explanations remain plausible."},
        drafts,
    )
    with pytest.raises(ValueError, match="Preview the current saved revision"):
        export_preview(wb, preview, state, values, "markdown")


def _post_callback(app, name, values, changed):
    key, callback = next(
        (key, value)
        for key, value in app.callback_map.items()
        if value.get("callback") and value["callback"].__wrapped__.__name__ == name
    )
    outputs = callback["output"]
    outputs = outputs if isinstance(outputs, list) else [outputs]
    output_records = [
        {"id": value.component_id, "property": value.component_property} for value in outputs
    ]
    payload = {
        "output": key,
        "outputs": output_records if len(output_records) > 1 else output_records[0],
        "inputs": [
            {**item, "value": values.get(item["id"] + "." + item["property"])}
            for item in callback["inputs"]
        ],
        "state": [
            {**item, "value": values.get(item["id"] + "." + item["property"])}
            for item in callback["state"]
        ],
        "changedPropIds": [changed],
    }
    response = app.server.test_client().post("/_dash-update-component", json=payload)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.json["response"]


def test_registered_review_callback_keeps_edits_then_saves_one_revision(wb):
    from swarm_detector.review_state import FIELD_IDS, FIELDS, values_from_review
    from swarm_detector.web import create_app

    app = create_app(wb)
    detail = wb.case_detail(wb.list_cases()[0]["case_id"])
    initial = values_from_review()
    values = {
        identifier + ".value": initial[field]
        for identifier, field in zip(FIELD_IDS, FIELDS, strict=True)
    }
    values.update(
        {
            "detail-store.data": detail,
            "drafts-store.data": {},
            "form-state.data": {},
            "review-tick.data": 0,
            "case-select.value": detail["case"]["case_id"],
            "save-review.n_clicks": 0,
            "discard-review.n_clicks": 0,
        }
    )
    loaded = _post_callback(app, "review_form", values, "detail-store.data")
    values["form-state.data"] = loaded["form-state"]["data"]
    values["drafts-store.data"] = loaded["drafts-store"]["data"]
    values["reviewer-name.value"] = "Browser analyst"
    values["review-reason.value"] = (
        "Repeated episodes support coordination; intent remains unknown."
    )
    edited = _post_callback(app, "review_form", values, "review-reason.value")
    assert edited["form-state"]["data"]["dirty"]
    assert "Unsaved changes" in edited["review-draft-status"]["children"]
    values["form-state.data"] = edited["form-state"]["data"]
    values["drafts-store.data"] = edited["drafts-store"]["data"]
    values["save-review.n_clicks"] = 1
    saved = _post_callback(app, "review_form", values, "save-review.n_clicks")
    assert saved["form-state"]["data"]["revision"] == 1
    assert not saved["form-state"]["data"]["dirty"]
    assert not saved["drafts-store"]["data"]
    assert wb.get_review(detail["case"]["case_id"])["reviewer"] == "Browser analyst"


def test_selected_pair_fetch_is_not_starved_by_the_global_event_page(wb):
    from swarm_detector.presentation import episode_options, with_selected_pair
    from swarm_detector.web import create_app

    case = wb.list_cases(kind="candidate")[0]
    detail = wb.case_detail(case["case_id"], limit=2)
    selected = detail["pairs"][-1]
    assert not selected["events"]
    app = create_app(wb)
    loader = app.callback_map["selected-pair-store.data"]["callback"].__wrapped__
    selection = loader(selected["pair_key"], detail)
    enriched = with_selected_pair(detail, selection)
    pair = next(p for p in enriched["pairs"] if p["pair_key"] == selected["pair_key"])
    assert pair["events"]
    assert selection["run_id"] == detail["run"]["run_id"]
    assert episode_options(enriched, selected["pair_key"])
