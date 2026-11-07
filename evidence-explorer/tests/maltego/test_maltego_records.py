"""Lookup records are frozen, hashed, verifiable, and become citable saved sources."""

import hashlib
import json
import os
import stat

import pytest
from pydantic import ValidationError

from evidence_explorer.collection.maltego.records import (
    EMPTY_RESULT_TEXT,
    ENTITY_RENDERING,
    SOURCE_TEXT_LIMIT,
    TEXT_RENDERING,
    MaltegoLookupRecord,
    RecordStore,
    build_record,
    build_source_import,
    canonical_json,
    is_access_denied,
    render_result_text,
    to_source_item,
)
from evidence_explorer.collection.maltego.transport import ServerIdentity, ToolCall
from evidence_explorer.collection.models import CollectionFailure
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.source_bundles import build_source_bundle

SERVER = ServerIdentity("2025-06-18", "Fixture Data MCP", "9.9", None)
RESOURCE = "https://datamcp.example/mcp"
WHOIS = {
    "status": "ok",
    "run_id": "run-1",
    "run_status": "SUCCEEDED",
    "count": 1,
    "entities": [
        {
            "ref": "run-1#e1",
            "type": "maltego.WHOISRecord",
            "value": "example.org",
            "creationDate": "2001-02-03 04:05:06 UTC",
            "registrar": "Example Registrar, LLC",
            "nameservers": "ns1.example.net, ns2.example.net",
        }
    ],
    "credits": {"estimate": True, "period_consumed_total": 5, "period_remaining": 49990},
}
CONTEXT = {
    "capability_id": "ci_whois",
    "capability_name": "[CTI] To WHOIS Records [Fixture]",
    "input": {"entity_type": "maltego.Domain", "entity_value": "example.org"},
}
RUN_ARGUMENTS = {
    "capability_id": "ci_whois",
    "entity_type": "maltego.Domain",
    "entity_value": "example.org",
}


def text(value, *, error=False):
    return {"content": [{"type": "text", "text": value}], "isError": error}


def call(result, *, tool="maltego_run", arguments=None, completed="2026-11-07T12:00:03.000Z"):
    return ToolCall(
        tool,
        RUN_ARGUMENTS if arguments is None else arguments,
        result,
        "2026-11-07T12:00:00.000Z",
        completed,
        SERVER,
        (),
    )


def whois_record(**changes):
    return build_record(call(text(json.dumps(WHOIS)), **changes), RESOURCE, CONTEXT)


def test_entity_results_get_a_verbatim_listing_and_reproducible_digests():
    record = whois_record()
    assert record.rendering == ENTITY_RENDERING and record.evidence_kind == "entities"
    assert record.text.splitlines()[:4] == [
        "status: ok",
        "run_id: run-1",
        "run_status: SUCCEEDED",
        "count: 1",
    ]
    assert "Entity 1 · maltego.WHOISRecord · example.org\nref: run-1#e1" in record.text
    assert "registrar: Example Registrar, LLC" in record.text
    assert 'credits: {"estimate":true,"period_consumed_total":5,"period_remaining":49990}' in (
        record.text
    )
    assert record.text_sha256 == hashlib.sha256(record.text.encode()).hexdigest()
    assert (
        record.result_sha256 == hashlib.sha256(canonical_json(record.result).encode()).hexdigest()
    )
    assert whois_record() == record
    later = whois_record(completed="2026-11-07T12:00:04.000Z")
    assert later.record_id != record.record_id and later.text_sha256 == record.text_sha256


def test_other_results_keep_text_verbatim_and_omit_binary_payloads():
    result = {
        "content": [
            {"type": "text", "text": "  spacing\tkept "},
            {"type": "image", "data": "SECRETBYTES", "mimeType": "image/png"},
            {
                "type": "resource",
                "resource": {"uri": "maltego://e/1", "mimeType": "application/json", "text": "{}"},
            },
            {
                "type": "resource",
                "resource": {"uri": "maltego://b/2", "mimeType": "application/pdf", "blob": "QQ=="},
            },
            {"type": "resource_link", "name": "Report", "uri": "https://example.org/r"},
            {"type": "mystery"},
        ]
    }
    rendered = render_result_text(result)
    assert rendered.split("\n\n")[0] == "  spacing\tkept "
    assert "SECRETBYTES" not in rendered and "QQ==" not in rendered
    for expected in (
        "[Image omitted: image/png]",
        "[Embedded resource maltego://e/1 (application/json)]\n{}",
        "[Binary resource omitted: maltego://b/2 (application/pdf)]",
        "[Resource link: Report https://example.org/r]",
        "[Unsupported content block: mystery]",
    ):
        assert expected in rendered
    structured = {"content": [], "structuredContent": {"b": 1, "a": [1]}}
    assert render_result_text(structured) == json.dumps({"a": [1], "b": 1}, indent=2)
    assert render_result_text({"content": []}) == EMPTY_RESULT_TEXT
    search = build_record(
        call(text("{}"), tool="maltego_search", arguments={"goal": "who"}), RESOURCE
    )
    assert search.rendering == TEXT_RENDERING and search.evidence_kind == "discovery"


@pytest.mark.parametrize(
    "field, value",
    [
        ("text", "registrar: Someone Else"),
        ("result", text("{}")),
        ("arguments", {"entity_value": "example.net"}),
        ("context", {}),
        ("completed_at", "2026-11-08T00:00:00Z"),
        ("is_error", True),
        ("rendering", TEXT_RENDERING),
    ],
)
def test_any_edit_to_a_saved_record_is_rejected(tmp_path, field, value):
    record = whois_record()
    store = RecordStore(tmp_path)
    path = store.save(record)
    data = json.loads(path.read_text())
    data[field] = value
    os.chmod(path, 0o644)
    path.write_text(json.dumps(data))
    with pytest.raises(CollectionFailure, match="integrity check"):
        store.load(record.record_id)
    assert [row["verified"] for row in store.inventory()] == [False]


def test_constructing_a_record_with_a_wrong_digest_fails():
    data = whois_record().model_dump(mode="json")
    data["text_sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        MaltegoLookupRecord.model_validate(data)


def test_the_store_writes_once_read_only_and_ignores_itself_in_git(tmp_path):
    record = whois_record()
    store = RecordStore(tmp_path)
    path = store.save(record)
    assert store.save(record) == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert (tmp_path / "maltego" / ".gitignore").read_text().splitlines()[-1] == "*"
    assert store.load(record.record_id) == record and store.ids() == [record.record_id]
    for bad in ("../evidence", "A" * 64, record.record_id[:-1]):
        with pytest.raises(CollectionFailure):
            store.path(bad)


def test_a_record_stored_under_another_id_is_rejected(tmp_path):
    record = whois_record()
    store = RecordStore(tmp_path)
    path = store.save(record)
    other = "0" * 64
    (store.root / f"{other}.json").write_text(path.read_text())
    with pytest.raises(CollectionFailure, match="another ID"):
        store.load(other)


def test_entity_records_become_saved_sources_whose_exact_citations_verify():
    record = whois_record()
    denied = build_record(call(text("Access denied", error=True)), RESOURCE, CONTEXT)
    search = build_record(
        call(text("{}"), tool="maltego_search", arguments={"goal": "who"}), RESOURCE
    )
    assert is_access_denied(denied) and not is_access_denied(record)
    source_import = build_source_import(
        [record, denied, search, record],
        title="Who registered example.org?",
        question="Which registrar does WHOIS report for example.org?",
    )
    assert len(source_import.items) == 1
    item = source_import.items[0]
    assert (item.platform, item.channel_id, item.channel_name, item.native_id) == (
        "maltego",
        "datamcp/ci_whois",
        "[CTI] To WHOIS Records [Fixture]",
        record.record_id,
    )
    assert (
        item.title == "[CTI] To WHOIS Records [Fixture] (ci_whois) · on maltego.Domain example.org"
    )
    assert (item.provenance.method, item.provenance.content_scope) == (
        "maltego_datamcp",
        "lookup_result",
    )
    assert item.published_at is None and item.source_url is None

    bundle = build_source_bundle(source_import)
    saved = next(entry for entry in bundle.records if entry.kind == "saved_source_item")
    assert saved.content_hash == record.text_sha256
    assert saved.source_label == "Maltego · [CTI] To WHOIS Records [Fixture]"
    quote = "registrar: Example Registrar, LLC"
    start = saved.text.index(quote)

    def cite(supplied, offset):
        data = bundle.model_dump(mode="json")
        selector = {"kind": "text", "start": offset, "end": offset + len(quote)}
        data["fragments"].append(
            {"id": "claim", "record_id": saved.id, "selector": selector, "quote": supplied}
        )
        return EvidenceBundle.model_validate(data)

    cite(quote, start)
    with pytest.raises(ValidationError):
        cite("registrar: Planted Registrar Inc", start)
    with pytest.raises(ValidationError):
        cite(quote, start + 1)


def test_discovery_and_error_records_need_explicit_flags():
    denied = build_record(call(text("Access denied", error=True)), RESOURCE, CONTEXT)
    search = build_record(
        call(text("{}"), tool="maltego_search", arguments={"goal": "who"}), RESOURCE
    )
    with pytest.raises(CollectionFailure, match="result entities"):
        build_source_import([search, denied], title="Case", question="Question?")
    both = build_source_import(
        [search, denied],
        title="Case",
        question="Question?",
        include_errors=True,
        include_discovery=True,
    )
    assert len(both.items) == 2


def test_long_results_are_truncated_with_a_marker_and_a_stable_prefix():
    long_text = "x" * (SOURCE_TEXT_LIMIT + 5000)
    record = build_record(
        call(text(long_text), tool="maltego_describe", arguments={"ids": ["ci_a"]}), RESOURCE
    )
    item = to_source_item(record)
    assert len(item.text) == SOURCE_TEXT_LIMIT
    assert item.text.endswith(f"the full frozen Maltego record is {record.record_id}.]")
    kept = item.text.index("\n[Traced: text truncated")
    assert item.text[:kept] == record.text[:kept]
