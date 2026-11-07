"""Lookups, guards, and the command line work against the fixture Data MCP."""

import io
import json
import re
from types import SimpleNamespace

import httpx
import pytest

from evidence_explorer.collection.maltego import cli, service
from evidence_explorer.collection.maltego.service import MaltegoDataSource, refuse_credentials
from evidence_explorer.collection.models import CollectionFailure
from evidence_explorer.review.integrations.workspace import WorkspaceRepository
from evidence_explorer.source_contracts import SourceImport

ENTITY = {
    "ref": "run-1#e1",
    "type": "maltego.WHOISRecord",
    "value": "example.org",
    "registrar": "Example Registrar, LLC",
}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in (
        "TRACED_MALTEGO_MCP_URL",
        "TRACED_MALTEGO_CLIENT_ID",
        "TRACED_MALTEGO_CALLBACK_PORT",
        "TRACED_MALTEGO_TIMEOUT_SECONDS",
        "TRACED_MALTEGO_TOKEN_FILE",
        "EVIDENCE_READ_ONLY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")


@pytest.fixture
def source(fake, settings, signed_in, tmp_path):
    return MaltegoDataSource(
        settings,
        data_dir=tmp_path / "workspace",
        transport=fake.transport,
        token_store=signed_in,
        sleep=lambda _seconds: None,
    )


def entity_result(fake, request_id, *, entities=None, count=None, **fields):
    payload = {"status": "ok", "run_id": "run-1", **fields}
    if entities is not None:
        payload["entities"] = entities
        payload["count"] = len(entities) if count is None else count
    return httpx.Response(200, json=fake.rpc(request_id, fake.text_result(json.dumps(payload))))


def describe_result(fake, request_id, capability_id):
    item = {
        "id": capability_id,
        "kind": "transform",
        "name": "[CTI] To WHOIS Records [Fixture]",
        "description": "Latest WHOIS records.",
        "input_types": ["maltego.Domain"],
        "output_types": {"effective": ["maltego.WHOISRecord"]},
    }
    payload = {"items": [item], "entity_types": [], "not_found": [], "warnings": []}
    return httpx.Response(200, json=fake.rpc(request_id, fake.text_result(json.dumps(payload))))


def run_cli(source, *argv, sleep=lambda _seconds: None):
    out = io.StringIO()
    code = cli.main(
        list(argv),
        data_dir=source.records.root.parent.parent,
        source_factory=lambda _settings, _directory: source,
        out=out,
        sleep=sleep,
    )
    return code, out.getvalue()


def test_a_quick_lookup_describes_runs_and_freezes_linked_records(fake, source):
    calls = []

    def respond(request_id, name, arguments):
        calls.append((name, arguments))
        if name == "maltego_describe":
            return describe_result(fake, request_id, arguments["ids"][0])
        credits = {"estimate": True, "period_remaining": 49990}
        return entity_result(fake, request_id, entities=[ENTITY], credits=credits)

    fake.on_call = respond
    outcome = source.lookup(
        "ci_whois", entity_type="maltego.Domain", entity_value="example.org", impression_id="i-1"
    )
    assert [name for name, _arguments in calls] == ["maltego_describe", "maltego_run"]
    run_arguments = calls[1][1]
    assert run_arguments["capability_id"] == "ci_whois" and run_arguments["impression_id"] == "i-1"
    assert run_arguments["idempotency_key"] == outcome.idempotency_key
    assert outcome.idempotency_key.startswith("traced-")
    assert (outcome.status, outcome.run_id, outcome.entity_count) == ("ok", "run-1", 1)
    assert [record.evidence_kind for record in outcome.records] == ["discovery", "entities"]
    evidence = outcome.evidence[0]
    assert evidence.context == {
        "capability_id": "ci_whois",
        "capability_name": "[CTI] To WHOIS Records [Fixture]",
        "input": {"entity_type": "maltego.Domain", "entity_value": "example.org"},
    }
    assert outcome.credits == {"estimate": True, "period_remaining": 49990}
    assert source.records.load(evidence.record_id) == evidence


def test_a_long_run_is_polled_and_every_result_page_is_frozen(fake, source):
    polls = []

    def respond(request_id, name, arguments):
        if name == "maltego_describe":
            return describe_result(fake, request_id, "ci_whois")
        if name == "maltego_run":
            return entity_result(fake, request_id, status="running", poll_after_seconds=0)
        polls.append(arguments)
        if len(polls) == 1:
            return entity_result(
                fake, request_id, status="running", run_status="RUNNING", poll_after_seconds=0
            )
        offset = arguments.get("entities_offset", 0)
        page = [dict(ENTITY, ref=f"run-1#e{offset + 1}", value=f"page-{offset}")]
        return entity_result(fake, request_id, run_status="SUCCEEDED", entities=page, count=2)

    fake.on_call = respond
    outcome = source.lookup("ci_whois", entity_type="maltego.Domain", entity_value="example.org")
    assert polls[0] == {"run_id": "run-1", "wait_seconds": 20, "verbosity": "detailed"}
    assert [poll.get("entities_offset") for poll in polls] == [None, None, 1]
    assert (outcome.status, outcome.entity_count, len(outcome.evidence)) == ("ok", 2, 2)
    statuses = [record for record in outcome.records if record.tool_name == "maltego_run_status"]
    assert all(record.context["run_id"] == "run-1" for record in statuses)
    assert all(record.context["capability_id"] == "ci_whois" for record in statuses)
    assert outcome.evidence[1].text.count("Entity 1 · maltego.WHOISRecord · page-1") == 1


def test_a_run_beyond_the_deadline_returns_a_resume_hint(fake, source, monkeypatch):
    ticks = iter([0.0])
    clock = SimpleNamespace(monotonic=lambda: next(ticks, 10_000.0))
    monkeypatch.setattr(service, "time", clock)
    fake.on_call = lambda request_id, name, arguments: entity_result(
        fake, request_id, status="running"
    )
    outcome = source.lookup(
        "ci_whois", entity_type="maltego.Domain", entity_value="example.org", describe=False
    )
    assert outcome.status == "running" and outcome.evidence == ()
    assert "./traced maltego wait run-1" in outcome.notes[0]


def test_wait_resumes_an_earlier_run_by_id(fake, source):
    fake.on_call = lambda request_id, name, arguments: entity_result(
        fake, request_id, run_status="SUCCEEDED", entities=[ENTITY]
    )
    outcome = source.wait_for_run("run-1")
    assert (outcome.status, outcome.entity_count) == ("ok", 1)
    assert outcome.records[0].context == {"run_id": "run-1"}


def test_catalog_reloads_and_credentials_are_refused_before_any_request(fake, source):
    with pytest.raises(CollectionFailure, match="catalog reloads"):
        source.run("maltego_refresh_catalog", {})
    with pytest.raises(CollectionFailure, match="settings.apiKey") as caught:
        source.lookup(
            "ci_whois",
            entity_type="maltego.Domain",
            entity_value="example.org",
            settings={"apiKey": "fixture-secret"},
        )
    assert "fixture-secret" not in str(caught.value)
    assert fake.requests == []


@pytest.mark.parametrize(
    "value",
    [
        {"idempotency_key": "k", "entity_properties": {"person.lastname": "Doe"}},
        {"ids": ["ci_a"], "goal": "who operates this domain"},
    ],
)
def test_ordinary_arguments_pass_the_credential_guard(value):
    refuse_credentials(value)


@pytest.mark.parametrize(
    "value",
    [
        {"settings": {"API_KEY": "x"}},
        {"a": [{"password": "x"}]},
        {"Authorization": "Bearer x"},
        {"client_secret": "x"},
    ],
)
def test_credential_like_arguments_are_refused(value):
    with pytest.raises(CollectionFailure):
        refuse_credentials(value)


@pytest.mark.parametrize(
    "capability, entity",
    [
        ("whois", {"entity_type": "maltego.Domain", "entity_value": "example.org"}),
        ("ci_whois", {"entity_type": "maltego.Domain"}),
        ("ci_whois", {}),
    ],
)
def test_a_lookup_needs_a_catalog_id_and_a_complete_entity(fake, source, capability, entity):
    with pytest.raises(CollectionFailure):
        source.lookup(capability, **entity)
    assert fake.requests == []


def test_status_check_and_tools_never_print_tokens(fake, source):
    code, output = run_cli(source, "status")
    assert code == 0 and "Signed in" in output
    code, checked = run_cli(source, "check")
    assert code == 0 and "3 tools: maltego_search, maltego_describe, maltego_run" in checked
    code, described = run_cli(source, "tools", "maltego_run")
    assert code == 0 and "query (string, required): What to look up" in described
    for text in (output, checked, described):
        assert "access-1" not in text and "refresh-1" not in text


def test_call_retries_access_denied_once_and_keeps_both_records(fake, source):
    answers = iter(
        [
            fake.text_result("Access denied: entitlements are loading", error=True),
            fake.text_result('{"catalog": {"state": "ready"}}'),
        ]
    )
    fake.on_call = lambda request_id, name, arguments: httpx.Response(
        200, json=fake.rpc(request_id, next(answers))
    )
    waits = []
    code, output = run_cli(source, "call", "maltego_catalog_status", sleep=waits.append)
    assert code == 0 and waits == [60]
    assert "Access denied" in output and len(source.records.ids()) == 2


def test_search_run_and_case_round_trip_through_the_saved_source_import(fake, source, tmp_path):
    def respond(request_id, name, arguments):
        if name == "maltego_search":
            variant = {
                "catalog_id": "ci_whois",
                "display_name": "[CTI] To WHOIS Records [Fixture]",
                "input_type": "maltego.Domain",
            }
            result = {
                "rank": 1,
                "capability": "fixture.whois",
                "score": 0.9,
                "input_types": ["maltego.Domain"],
                "target_variants": [variant],
            }
            payload = {"objective_matches": {"results": [result]}, "impression_id": "imp-9"}
            return httpx.Response(
                200, json=fake.rpc(request_id, fake.text_result(json.dumps(payload)))
            )
        if name == "maltego_describe":
            return describe_result(fake, request_id, "ci_whois")
        credits = {"period_remaining": 49990}
        return entity_result(fake, request_id, entities=[ENTITY], credits=credits)

    fake.on_call = respond
    code, output = run_cli(
        source, "search", "Who registered this domain?", "--type", "maltego.Domain"
    )
    assert code == 0 and "imp-9" in output
    assert "ci_whois  [CTI] To WHOIS Records [Fixture] (on maltego.Domain)" in output
    code, output = run_cli(
        source, "run", "ci_whois", "--type", "maltego.Domain", "--value", "example.org"
    )
    assert code == 0 and "registrar: Example Registrar, LLC" in output
    assert "remaining 49990" in output and "--idempotency-key traced-" in output
    evidence = [row for row in source.records.inventory() if row["kind"] == "entities"]
    assert len(evidence) == 1
    out_file = tmp_path / "case.json"
    code, output = run_cli(
        source,
        "case",
        *source.records.ids(),
        "--title",
        "Who registered example.org?",
        "--question",
        "Which registrar does WHOIS report?",
        "--out",
        str(out_file),
        "--no-import",
    )
    assert code == 0 and "Left out 2" in output
    imported = SourceImport.model_validate_json(out_file.read_text())
    assert [item.native_id for item in imported.items] == [evidence[0]["record_id"]]
    code, output = run_cli(source, "verify")
    assert code == 0 and "0 of 3 saved records failed verification." in output


def test_case_import_creates_a_reviewable_case_in_the_workspace(fake, source):
    fake.on_call = lambda request_id, name, arguments: entity_result(
        fake, request_id, entities=[ENTITY]
    )
    outcome = source.lookup(
        "ci_whois", entity_type="maltego.Domain", entity_value="example.org", describe=False
    )
    record = outcome.evidence[0]
    code, output = run_cli(
        source, "case", record.record_id, "--title", "Example case", "--question", "Who?"
    )
    assert code == 0
    case_id = re.search(r"case (sources:case:[0-9a-f]{64})", output).group(1)
    workspace = source.records.root.parent.parent
    bundle, _fingerprint = WorkspaceRepository(workspace / "evidence.sqlite3").get_bundle_snapshot(
        case_id
    )
    saved = next(entry for entry in bundle.records if entry.kind == "saved_source_item")
    assert saved.content_hash == record.text_sha256 and saved.text == record.text


def test_an_invalid_run_is_reported_with_exit_code_3(fake, source):
    payload = json.dumps({"status": "invalid", "repair": "Pass entity_value"})
    fake.on_call = lambda request_id, name, arguments: httpx.Response(
        200, json=fake.rpc(request_id, fake.text_result(payload))
    )
    code, output = run_cli(
        source,
        "run",
        "ci_whois",
        "--type",
        "maltego.Domain",
        "--value",
        "example.org",
        "--no-describe",
    )
    assert code == 3 and "status invalid" in output


def test_guard_failures_are_reported_without_a_traceback(fake, source, capsys):
    code, _output = run_cli(source, "call", "maltego_refresh_catalog")
    assert code == 2 and "catalog reloads" in capsys.readouterr().err


def test_validate_only_checks_input_without_a_run_or_idempotency_key(fake, source):
    calls = []

    def respond(request_id, name, arguments):
        calls.append(arguments)
        payload = json.dumps({"status": "valid", "estimated_steps": 1, "estimated_seconds": 15})
        return httpx.Response(200, json=fake.rpc(request_id, fake.text_result(payload)))

    fake.on_call = respond
    code, output = run_cli(
        source,
        "run",
        "ci_whois",
        "--type",
        "maltego.Domain",
        "--value",
        "example.org",
        "--no-describe",
        "--validate-only",
    )
    assert code == 0 and "status valid" in output and "--idempotency-key" not in output
    assert calls == [
        {
            "capability_id": "ci_whois",
            "entity_type": "maltego.Domain",
            "entity_value": "example.org",
            "validate_only": True,
        }
    ]
