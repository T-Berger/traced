"""Composed review desk keeps media, imports, and downloads source-checkable."""

import base64
import hashlib
import json
import sqlite3
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from zipfile import ZipFile

import pytest
from dash import html
from dash.development.base_component import Component

from evidence_explorer.config import Settings
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.integrations import web
from evidence_explorer.review.integrations.handoff_service import load_handoff_context
from evidence_explorer.review.integrations.packages import export_case_package, import_case_package
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService


def components(node):
    if isinstance(node, Component):
        yield node
        yield from components(getattr(node, "children", None))
    elif isinstance(node, (list, tuple)):
        for child in node:
            yield from components(child)


def callback(app, output):
    return next(
        item["callback"].__wrapped__ for key, item in app.callback_map.items() if output in key
    )


@pytest.fixture
def desk(tmp_path):
    settings = Settings(data_dir=tmp_path / "workspace")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, settings.data_dir / "artifacts")
    service.seed_examples(include_swarm=False)
    app = web.create_app(service)
    return service, app, app.server.test_client()


def media_case(service):
    case_id = next(case["id"] for case in service.list_cases() if case["id"].startswith("media:"))
    bundle = service.repository.get_bundle(case_id)
    record = next(record for record in bundle.records if record.artifact_ref)
    return case_id, record


def post_upload(app, client, contents):
    output = next(key for key in app.callback_map if "trail-import-message.children" in key)
    return client.post(
        "/_dash-update-component",
        json={
            "output": output,
            "outputs": [
                {"id": "rv-case", "property": "options"},
                {"id": "rv-case", "property": "value"},
                {"id": "trail-import-message", "property": "children"},
            ],
            "inputs": [
                {"id": "trail-upload", "property": "contents", "value": contents},
                {"id": "trail-explorer", "property": "n_clicks", "value": 0},
            ],
            "state": [],
            "changedPropIds": ["trail-upload.contents"],
        },
    )


def upload_bytes(app, client, payload):
    contents = "data:application/octet-stream;base64," + base64.b64encode(payload).decode("ascii")
    response = post_upload(app, client, contents)
    assert response.status_code == 200, response.data
    return response.get_json()["response"]


def case_zip(case_id, files, *, manifest_files=None):
    bundle_hash = hashlib.sha256(files["bundle.json"]).hexdigest()
    manifest = {
        "format": "evidence-trail-case/2"
        if "planning-briefs.json" in files
        else "evidence-trail-case/1",
        "case_id": case_id,
        "bundle_fingerprint": bundle_hash,
        "files": manifest_files
        if manifest_files is not None
        else {
            name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in files.items()
        },
    }
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, data in files.items():
            archive.writestr(name, data)
    return output.getvalue()


def test_composed_page_loads_add_controls_once_without_creating_review_state(desk):
    service, app, client = desk
    before = {
        case["id"]: service.repository.list_events(case["id"]) for case in service.list_cases()
    }
    for _ in range(3):
        assert client.get("/").status_code == 200
        response = client.get("/_dash-layout")
        assert response.status_code == 200
        nodes = list(components(app.layout()))
        for component_id in (
            "trail-upload",
            "trail-explorer",
            "trail-download",
            "trail-package",
            "trail-json",
            "trail-brief",
            "trail-media",
            "trail-recompute",
            "trail-episode",
            "trail-result",
            "trail-original",
        ):
            assert sum(getattr(node, "id", None) == component_id for node in nodes) == 1
        mode = next(node for node in nodes if getattr(node, "id", None) == "rv-challenge-mode")
        assert sum(option["value"] == "ollama" for option in mode.options) == 1
        timer = next(node for node in nodes if getattr(node, "id", None) == "trail-tick")
        assert timer.disabled is True
    assert client.get("/health").get_json()["status"] == "ok"
    assert {
        case["id"]: service.repository.list_events(case["id"]) for case in service.list_cases()
    } == before
    with sqlite3.connect(service.repository.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_scenarios").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM review_jobs").fetchone()[0] == 0


def test_media_route_returns_only_hash_verified_referenced_artifacts(desk):
    service, _, client = desk
    case_id, record = media_case(service)
    response = client.get(
        "/trail-artifact", query_string={"case_id": case_id, "record_id": record.id}
    )
    assert response.status_code == 200
    assert response.mimetype == "image/svg+xml"
    assert response.data == service.artifact(case_id, record.id).read_bytes()
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in response.headers["Content-Security-Policy"]
    for query in (
        {"case_id": "absent", "record_id": record.id},
        {"case_id": case_id, "record_id": "../../private-file"},
        {"case_id": load_demo_bundle().case.id, "record_id": "harbor:r01"},
    ):
        assert client.get("/trail-artifact", query_string=query).status_code == 404


@pytest.mark.parametrize("assessment", ["scenario", "local_baseline", "imported_baseline"])
@pytest.mark.parametrize("picker", ["empty", "different", "already_selected"])
def test_session_only_updates_assessment_selector_when_necessary(desk, assessment, picker):
    service, app, client = desk
    case_id = load_demo_bundle().case.id
    baseline = assessment != "scenario"
    if assessment == "imported_baseline":
        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures/case_packages/mixed-reviewed-reference.zip"
        )
        case_id = service.import_package(fixture.read_bytes())
        (scenario,) = (
            item
            for item in service.assessments(case_id)
            if item["imported"] and item["is_baseline"]
        )
    elif baseline:
        scenario = service.baseline_scenario(case_id)
    else:
        scenario = service.start_scenario(case_id)
    selected = "baseline" if baseline else scenario["id"]
    current = {
        "empty": None,
        "different": scenario["id"] if baseline else "baseline",
        "already_selected": selected,
    }[picker]
    key = next(
        key
        for key, item in app.callback_map.items()
        if "rv-scenario.value" in key and {"id": "rv-session", "property": "data"} in item["inputs"]
    )
    registration = next(item for item in app._callback_list if item["output"] == key)
    assert registration["prevent_initial_call"] is False
    state = {"case_id": case_id, "scenario_id": scenario["id"]}
    response = client.post(
        "/_dash-update-component",
        json={
            "output": key,
            "outputs": {"id": "rv-scenario", "property": "value"},
            "inputs": [
                {"id": "trail-operation-message", "property": "children", "value": None},
                {"id": "rv-session", "property": "data", "value": state},
            ],
            "state": [{"id": "rv-scenario", "property": "value", "value": current}],
            "changedPropIds": ["rv-session.data"],
        },
    )
    assert response.status_code == 200
    output = response.get_json()["response"]
    if picker == "already_selected":
        assert "rv-scenario" not in output
    else:
        assert output["rv-scenario"]["value"] == selected


@pytest.mark.parametrize("change", ["tampered", "missing"])
def test_changed_or_missing_media_is_not_served_under_its_old_evidence_identity(desk, change):
    service, _, client = desk
    case_id, record = media_case(service)
    path = service.artifact(case_id, record.id)
    if change == "tampered":
        path.write_text("Different content", encoding="utf-8")
    else:
        path.unlink()
    response = client.get(
        "/trail-artifact", query_string={"case_id": case_id, "record_id": record.id}
    )
    assert response.status_code == 404
    assert b"Different content" not in response.data


def test_media_callback_points_to_the_supplied_record_and_preserves_unknown_context(desk):
    from evidence_explorer.review.integrations.media_ui import transition_media

    service, app, client = desk
    case_id, record = media_case(service)
    state = {"case_id": case_id, "record_id": record.id}
    hidden, store = callback(app, "trail-media.children")(state)
    assert not any(isinstance(node, html.Img) for node in components(hidden))
    assert store["status"] == "hidden"
    reveal = next(
        node.id
        for node in components(hidden)
        if isinstance(node, html.Button)
        and isinstance(node.id, dict)
        and node.id.get("action") == "reveal"
    )
    view, store = transition_media(service, state, reveal, clicks=[1], stored=store)
    image = next(node for node in components(view) if isinstance(node, html.Img))
    query = parse_qs(urlparse(image.src).query)
    assert query["case_id"] == [case_id]
    assert query["record_id"] == [record.id]
    assert query["attempt"] == [store["attempt"]]
    assert client.get(image.src).status_code == 200
    rendered = json.dumps(view, default=lambda node: node.to_plotly_json())
    assert "First-posted exercise week" in rendered
    assert "Posting-channel affiliation" in rendered
    assert "Capture or event time" in rendered
    assert "Unknown" in rendered
    assert "Original publication context" in rendered
    assert "unavailable" in rendered


@pytest.mark.parametrize(
    ("trigger", "filename"),
    [
        ("trail-package", "evidence-trail-case.zip"),
        ("trail-json", "evidence-trail-case.json"),
        ("trail-brief", "evidence-trail-brief.md"),
    ],
)
def test_download_buttons_export_the_selected_case(desk, monkeypatch, tmp_path, trigger, filename):
    service, app, _ = desk
    case_id, _ = media_case(service)
    monkeypatch.setattr(web, "ctx", SimpleNamespace(triggered_id=trigger))
    state = session_for_case(service, case_id)
    result, attempt = callback(app, "trail-download.data")(
        1, 1, 1, state, None, None, load_handoff_context(service, state)[1]["download_context"]
    )
    assert attempt["status"] == "prepared"
    assert result["filename"] == filename
    content = base64.b64decode(result["content"]) if result["base64"] else result["content"]
    if trigger == "trail-package":
        reopened = import_case_package(content, tmp_path / "downloaded-artifacts")
        assert reopened.case.id == case_id
        assert any(record.artifact_ref for record in reopened.records)
    elif trigger == "trail-json":
        assert EvidenceBundle.model_validate_json(content).case.id == case_id
    else:
        assert service.get_case(case_id)["case"]["title"] in content
        assert "Selected case: Synthetic example" in content
        assert "fictional evidence and findings" not in content.lower()


@pytest.mark.parametrize("kind", ["json", "zip"])
def test_uploaded_cases_import_through_the_registered_dash_callback(desk, kind):
    service, app, client = desk
    if kind == "json":
        raw = load_demo_bundle().model_dump(mode="json")
        case_id = raw["case"]["id"] = "uploaded:json-case"
        payload = json.dumps(raw).encode("utf-8")
    else:
        case_id, _ = media_case(service)
        payload = service.export_package(case_id)
    count = len(service.list_cases())
    contents = "data:application/octet-stream;base64," + base64.b64encode(payload).decode("ascii")
    response = post_upload(app, client, contents)
    assert response.status_code == 200, response.data
    result = response.get_json()["response"]
    assert result["rv-case"]["value"] == case_id
    assert case_id in {option["value"] for option in result["rv-case"]["options"]}
    assert len(service.list_cases()) == count + (kind == "json")
    assert "Case imported" in json.dumps(result["trail-import-message"])


@pytest.mark.parametrize(
    ("contents", "reason"),
    [
        ("data:application/json;base64,%%%", "Select the case file again"),
        (
            "data:application/json;base64," + base64.b64encode(b"{}").decode("ascii"),
            "required field is missing",
        ),
        (
            "data:application/json;base64," + base64.b64encode(b"PRIVATE_JSON").decode("ascii"),
            "The evidence JSON could not be read",
        ),
        (
            "data:application/zip;base64," + base64.b64encode(b"PK-not-a-case-archive").decode(),
            "Invalid evidence package",
        ),
    ],
)
def test_invalid_upload_reports_error_without_changing_the_case_list(desk, contents, reason):
    service, app, client = desk
    before = service.list_cases()
    response = post_upload(app, client, contents)
    assert response.status_code == 200, response.data
    result = response.get_json()["response"]
    notice = result["trail-import-message"]["children"]["props"]
    assert "rv-notice--error" in notice["className"]
    assert reason in notice["children"] and "again" in notice["children"]
    assert "PRIVATE_JSON" not in notice["children"] and "line 1 column" not in notice["children"]
    assert "rv-case" not in result
    assert service.list_cases() == before


@pytest.mark.parametrize("kind", ["json", "zip"])
@pytest.mark.parametrize(
    ("fragment_id", "expected_fragment"),
    [
        (
            "media:synthetic-history:frame:alpha:image",
            "“media:synthetic-history:frame:alpha:image”",
        ),
        ("<img src=x>\nPrivate input", None),
        ("a" * 201, None),
    ],
)
def test_unresolved_upload_explains_missing_record_and_accepts_corrected_retry(
    desk, kind, fragment_id, expected_fragment
):
    service, app, client = desk
    before = service.list_cases()
    raw = load_demo_bundle().model_dump(mode="json")
    case_id = raw["case"]["id"] = "uploaded:corrected-case"
    valid_bundle = EvidenceBundle.model_validate(raw)
    valid = valid_bundle.model_dump_json().encode()
    raw["fragments"][0].update(
        id=fragment_id, record_id="missing-record", quote="Private source excerpt"
    )
    invalid = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    if kind == "zip":
        invalid = case_zip(case_id, {"bundle.json": invalid})
        valid = export_case_package(valid_bundle)

    rejected = upload_bytes(app, client, invalid)
    notice = rejected["trail-import-message"]["children"]["props"]["children"]
    fragment = (
        f"Evidence fragment {expected_fragment}"
        if expected_fragment
        else "A saved evidence fragment"
    )
    assert notice == (
        f"{fragment} refers to a missing record. "
        "Ask the sender for a corrected case file, then upload it again."
    )
    assert "rv-case" not in rejected
    assert service.list_cases() == before

    accepted = upload_bytes(app, client, valid)
    assert accepted["rv-case"]["value"] == case_id
    assert "Case imported" in json.dumps(accepted["trail-import-message"])
    assert len(service.list_cases()) == len(before) + 1
    assert service.repository.get_bundle(case_id) == valid_bundle


@pytest.mark.parametrize("member", ["manifest", "planning"])
def test_native_package_validation_does_not_reflect_producer_keys(desk, member):
    service, app, client = desk
    before = service.list_cases()
    bundle = load_demo_bundle()
    files = {
        "bundle.json": json.dumps(
            bundle.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
    }
    manifest_files = None
    if member == "manifest":
        manifest_files = {"PRIVATEPRODUCERKEY": {"bytes": "Private source excerpt"}}
    else:
        files["planning-briefs.json"] = b'{"PRIVATEPRODUCERKEY":"Private source excerpt"}'
    payload = case_zip(bundle.case.id, files, manifest_files=manifest_files)

    rejected = upload_bytes(app, client, payload)
    notice = rejected["trail-import-message"]["children"]["props"]["children"]
    assert notice == (
        "Invalid evidence package. Ask the sender for a corrected case file, then upload it again."
    )
    assert "rv-case" not in rejected
    assert service.list_cases() == before


@pytest.mark.parametrize("failure_stage", ["save", "receipt"])
def test_upload_io_failure_uses_stage_specific_recovery(desk, monkeypatch, failure_stage):
    from evidence_explorer.review.integrations import handoff_service

    service, app, client = desk
    before = service.list_cases()
    raw = load_demo_bundle().model_dump(mode="json")
    case_id = raw["case"]["id"] = "uploaded:receipt-io-failure"

    def fail(*args):
        raise OSError(13, "Private workspace details", "/private/fixture/path")

    if failure_stage == "save":
        monkeypatch.setattr(service, "import_bundle", fail)
    else:
        monkeypatch.setattr(handoff_service, "import_receipt_for", fail)

    response = upload_bytes(app, client, json.dumps(raw).encode())
    notice = response["trail-import-message"]["children"]["props"]["children"]
    assert "rv-case" not in response
    assert "Private workspace details" not in notice and "/private/fixture/path" not in notice
    if failure_stage == "save":
        assert "Check workspace access and try again" in notice
        assert service.list_cases() == before
    else:
        assert "The case was saved" in notice and "Reload the workspace" in notice
        assert case_id in {case["id"] for case in service.list_cases()}


def test_metadata_source_opens_its_documented_image_instead_of_an_unrelated_frame(desk):
    from evidence_explorer.review.integrations.media_ui import transition_media

    service, app, client = desk
    case_id, _ = media_case(service)
    bundle = service.repository.get_bundle(case_id)
    frame = next(
        record
        for record in bundle.records
        if record.artifact_ref and record.metadata["frame_id"] == "frame-b"
    )
    metadata_id = next(
        link.source.id
        for link in bundle.links
        if link.relation == "part_of"
        and link.target.type == "record"
        and link.target.id == frame.id
        and link.source.type == "record"
    )
    state = {"case_id": case_id, "record_id": metadata_id}
    hidden, store = callback(app, "trail-media.children")(state)
    assert not any(isinstance(node, html.Img) for node in components(hidden))
    reveal = next(
        node.id
        for node in components(hidden)
        if isinstance(node, html.Button)
        and isinstance(node.id, dict)
        and node.id.get("action") == "reveal"
    )
    preview, _ = transition_media(service, state, reveal, clicks=[1], stored=store)
    image = next(node for node in components(preview) if isinstance(node, html.Img))
    assert parse_qs(urlparse(image.src).query)["record_id"] == [frame.id]
    assert client.get(image.src).data == service.artifact(case_id, frame.id).read_bytes()
    clip = next(record for record in bundle.records if record.kind == "supplied_clip")
    body, store = callback(app, "trail-media.children")({"case_id": case_id, "record_id": clip.id})
    assert body == "" and store == {}


@pytest.mark.parametrize("status", [None, "queued", "running", "completed", "failed"])
def test_comparison_polls_only_while_a_computation_is_active(desk, status):
    service, app, _ = desk
    case_id = load_demo_bundle().case.id
    scenario = service.start_scenario(case_id)
    if status is not None:
        finding = service.repository.get_bundle(case_id).findings[0]
        job = service.repository.enqueue_job(
            case_id, finding.snapshot_id, scenario["id"], "swarm_recompute"
        )
        if status != "queued":
            running = service.repository.claim_job()
            assert running["id"] == job["id"]
            if status == "completed":
                service.repository.finish_job(
                    job["id"],
                    {"result_run_id": scenario["base_run_id"], "correspondences": []},
                    expected_attempt=running["attempts"],
                )
            elif status == "failed":
                service.repository.fail_job(
                    job["id"], "Visible failure", expected_attempt=running["attempts"]
                )
    result = callback(app, "trail-comparison.children")(
        {"case_id": case_id, "scenario_id": scenario["id"]}, 0
    )
    assert len(result) == 4
    assert result[3] is (status not in {"queued", "running"})
    assert result[2] is (status != "completed")
    if status == "failed":
        assert "Visible failure" in json.dumps(
            result[0], default=lambda node: node.to_plotly_json()
        )


def test_result_and_original_transitions_refresh_the_case_fingerprint(desk, monkeypatch):
    service, app, _ = desk
    case_id = load_demo_bundle().case.id
    baseline = service.repository.get_bundle(case_id)
    scenario = service.start_scenario(case_id)
    state = {
        "case_id": case_id,
        "finding_id": baseline.findings[0].snapshot_id,
        "record_id": baseline.records[0].id,
        "scenario_id": scenario["id"],
        "scenario_version": scenario["version"],
        "case_fingerprint": baseline.fingerprint(),
    }
    payload = baseline.model_dump(mode="json")
    result_run_id = "regression:result-run"
    payload["runs"].append(dict(payload["runs"][0], id=result_run_id))
    payload["findings"].append(
        dict(
            payload["findings"][0],
            snapshot_id="regression:result-finding",
            run_id=result_run_id,
            version=2,
        )
    )
    service.import_bundle(payload)
    job = service.repository.enqueue_job(
        case_id, state["finding_id"], scenario["id"], "swarm_recompute"
    )
    running = service.repository.claim_job()
    assert running["id"] == job["id"]
    service.repository.finish_job(
        job["id"], {"result_run_id": result_run_id}, expected_attempt=running["attempts"]
    )
    monkeypatch.setattr(web, "ctx", SimpleNamespace(triggered_id="trail-result"))
    action = callback(app, "trail-operation-message.children")
    result, _ = action(0, 0, 1, 0, state)
    assert result["finding_id"] == "regression:result-finding"
    assert service.repository.get_scenario(result["scenario_id"])["base_run_id"] == result_run_id
    assert result["case_fingerprint"] == service.case_fingerprint(case_id)
    assert result["case_fingerprint"] != state["case_fingerprint"]
    service.review(
        case_id,
        result["finding_id"],
        result["scenario_id"],
        "qualify",
        "The review follows the newly displayed result.",
        expected_version=result["scenario_version"],
        expected_fingerprint=result["case_fingerprint"],
    )
    monkeypatch.setattr(web, "ctx", SimpleNamespace(triggered_id="trail-original"))
    restored, _ = action(0, 1, 0, 0, result)
    assert restored["scenario_id"] is None
    assert restored["case_fingerprint"] == service.case_fingerprint(case_id)
    assert restored["finding_id"] in {finding.snapshot_id for finding in baseline.findings}


@pytest.mark.parametrize("terminal", ["completed", "failed"])
def test_swarm_recompute_uses_only_the_current_persisted_status(desk, monkeypatch, terminal):
    service, app, _ = desk
    state = {"case_id": "case", "finding_id": "finding", "scenario_id": "scenario"}
    job = {"id": "job", "status": "queued"}
    view = {
        "bundle": {
            "runs": [{"id": "baseline", "full_inputs_available": True}],
            "swarm_context": {"baseline_run_id": "baseline"},
        },
        "scenario": {"base_run_id": "baseline", "excluded_record_ids": ["record"]},
        "computation": job,
    }
    monkeypatch.setattr(service, "recompute", lambda *args: job)
    monkeypatch.setattr(service, "view", lambda *args: view)
    monkeypatch.setattr(web, "ctx", SimpleNamespace(triggered_id="trail-recompute"))

    action = callback(app, "trail-operation-message.children")
    updated, notice = action(1, 0, 0, 0, state)
    assert updated == {**state, "job_id": job["id"]}
    assert notice == ""

    compare = callback(app, "trail-comparison.children")
    queued, recompute_disabled, result_disabled, polling_disabled = compare(updated, 0)
    assert queued.children == "Swarm recomputation queued with the saved background activity."
    assert (recompute_disabled, result_disabled, polling_disabled) == (True, True, False)
    job["status"] = "running"
    running, _, _, polling_disabled = compare(updated, 1)
    assert running.children == "Swarm recomputation is running."
    assert polling_disabled is False

    job.update(
        status=terminal, result={"correspondences": [], "cached": False}, error="Detector failed."
    )
    finished, _, result_disabled, polling_disabled = compare(updated, 2)
    assert polling_disabled is True
    if terminal == "failed":
        assert finished.children == "Detector failed."
        assert "rv-notice--error" in finished.className
        assert result_disabled is True
    else:
        assert finished.children[0].children.startswith("Completed Swarm recomputation")
        assert result_disabled is False


@pytest.fixture(scope="module")
def swarm_bundle():
    detector = pytest.importorskip("swarm_detector.detector")
    source = pytest.importorskip("swarm_detector.sample_data")
    from evidence_explorer.review.integrations.swarm import build_swarm_bundle

    dataset = source.build_sample()
    return build_swarm_bundle(dataset, detector.analyze(dataset, {"randomizations": 5, "seed": 42}))


def test_strongest_episode_control_selects_context_without_creating_a_scenario(
    desk, monkeypatch, swarm_bundle
):
    from evidence_explorer.review.integrations.episodes import strongest_episode_record

    service, app, _ = desk
    case_id = service.import_bundle(swarm_bundle.model_dump(mode="json"))
    finding_id = swarm_bundle.findings[0].snapshot_id
    before = service.repository.list_events(case_id)
    monkeypatch.setattr(web, "ctx", SimpleNamespace(triggered_id="trail-episode"))
    state, _ = callback(app, "trail-operation-message.children")(
        0,
        0,
        0,
        1,
        {"case_id": case_id, "finding_id": finding_id, "scenario_id": None},
    )
    assert state["record_id"] == strongest_episode_record(swarm_bundle, finding_id).id
    assert state["scenario_id"] is None
    assert service.repository.list_events(case_id) == before
    assert callback(app, "trail-episode.disabled")(state) is False


def test_comparison_disables_recompute_for_a_derived_swarm_run(desk, swarm_bundle):
    service, app, _ = desk
    payload = swarm_bundle.model_dump(mode="json")
    derived_id = "regression:derived-run"
    payload["runs"].append(dict(payload["runs"][0], id=derived_id))
    finding_id = "regression:derived-finding"
    payload["findings"].append(
        dict(payload["findings"][0], snapshot_id=finding_id, run_id=derived_id, version=2)
    )
    case_id = service.import_bundle(payload)
    scenario = service.start_scenario(case_id, finding_id)
    service.exclude(
        scenario["id"], payload["records"][0]["id"], True, "Derived exclusion", scenario["version"]
    )
    state = {"case_id": case_id, "finding_id": finding_id, "scenario_id": scenario["id"]}
    compared = callback(app, "trail-comparison.children")(state, 0)
    assert compared[1:] == (True, True, True)
    assert callback(app, "trail-episode.disabled")(state) is True
