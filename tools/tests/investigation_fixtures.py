"""Fictional frozen Maltego records and fakes for the investigation tests (no network).

Records are built by the real ``build_record`` from fake tool calls, so they carry
real digests and render exactly like live entity results. Every value is fictional:
``example.org`` and documentation addresses (192.0.2.0/24, AS64500).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evidence_explorer.collection.maltego.records import (  # noqa: E402
    MaltegoLookupRecord,
    RecordStore,
    build_record,
)
from evidence_explorer.collection.maltego.service import LookupOutcome  # noqa: E402
from evidence_explorer.collection.maltego.transport import ServerIdentity, ToolCall  # noqa: E402

from tools.investigation.bundle import InvestigationDraft  # noqa: E402
from tools.investigation.evidence import index_lines  # noqa: E402

SERVER = ServerIdentity("2025-06-18", "datamcp-test", "1.0.0", None)
SERVER_URL = "https://datamcp.example.test/mcp"
GOAL = "Who is behind example.org?"
SUBJECT = {"entity_type": "maltego.Domain", "entity_value": "example.org"}


def entity_call(
    capability: str,
    name: str,
    input_type: str,
    value: str,
    entities: list[dict],
    run: str,
    consumed: int,
) -> MaltegoLookupRecord:
    payload = {
        "status": "ok",
        "run_id": run,
        "run_status": "SUCCEEDED",
        "count": len(entities),
        "entities": entities,
        "credits": {
            "estimate": True,
            "period_consumed_total": consumed,
            "period_remaining": 50_000 - consumed,
            "period_resets_at": "2026-12-01T00:00:00+00:00",
        },
    }
    call = ToolCall(
        tool_name="maltego_run",
        arguments={"capability_id": capability, "entity_type": input_type, "entity_value": value},
        result={"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False},
        requested_at="2026-11-07T10:00:00Z",
        completed_at="2026-11-07T10:00:05Z",
        server=SERVER,
    )
    context = {
        "capability_id": capability,
        "capability_name": name,
        "input": {"entity_type": input_type, "entity_value": value},
    }
    return build_record(call, SERVER_URL, context)


def whois_record() -> MaltegoLookupRecord:
    return entity_call(
        "ci_TestWhois",
        "[Test] To WHOIS Records",
        "maltego.Domain",
        "example.org",
        [
            {
                "type": "maltego.WHOISRecord",
                "value": "example.org",
                "ref": "run-whois#e1",
                "creationDate": "2009-03-14 10:00:00 UTC",
                "updatedDate": "2025-02-01 08:00:00 UTC",
                "registryExpiryDate": "2027-03-14 10:00:00 UTC",
                "domainName": "example.org",
                "nameservers": "ns1.example-dns.net, ns2.example-dns.net",
                "registrantOrganization": "Larkspur Holdings Ltd",
                "registrantCountry": "GB",
                "registrar": "Corvid Registry, Inc.",
                "registrarIanaId": "9999",
            }
        ],
        "run-whois",
        5,
    )


def history_record() -> MaltegoLookupRecord:
    return entity_call(
        "ci_TestWhoisHistory",
        "[Test] To Historical WHOIS Records",
        "maltego.Domain",
        "example.org",
        [
            {
                "type": "maltego.WHOISRecord",
                "value": "example.org",
                "ref": "run-history#e1",
                "creationDate": "2001-05-05 00:00:00 UTC",
                "registrantOrganization": "Fernhill Media plc",
                "registrar": "Corvid Registry, Inc.",
            }
        ],
        "run-history",
        12,
    )


def netblock_record() -> MaltegoLookupRecord:
    return entity_call(
        "ci_TestNetblock",
        "[Test] To Netblock Owner",
        "maltego.IPv4Address",
        "192.0.2.10",
        [
            {
                "type": "maltego.Netblock",
                "value": "192.0.2.0/24",
                "ref": "run-net#e1",
                "asn": "64500",
                "organization": "Quarry Hosting GmbH",
                "country": "DE",
                "abuseEmail": "abuse@quarry-hosting.test",
            }
        ],
        "run-net",
        7,
    )


def search_record() -> MaltegoLookupRecord:
    payload = {
        "objective_matches": {
            "objective": GOAL,
            "entity_type": "maltego.Domain",
            "results": [
                {
                    "rank": 1,
                    "capability": "test.whois",
                    "source": "test",
                    "question": "Who registered this domain?",
                    "score": 0.9,
                    "target_variants": [
                        {
                            "catalog_id": "ci_TestWhois",
                            "display_name": "[Test] To WHOIS Records",
                            "input_type": "maltego.Domain",
                        }
                    ],
                },
                {
                    "rank": 2,
                    "capability": "test.whois_history",
                    "source": "test",
                    "question": "",
                    "score": 0.8,
                    "target_variants": [
                        {
                            "catalog_id": "ci_TestWhoisHistory",
                            "display_name": "[Test] To Historical WHOIS Records",
                            "input_type": "maltego.Domain",
                        },
                        {
                            "catalog_id": "ci_WrongType",
                            "display_name": "[Test] Email variant",
                            "input_type": "maltego.EmailAddress",
                        },
                    ],
                },
            ],
        },
        "facets": {"popular_for_type": []},
        "impression_id": "impression-1",
    }
    call = ToolCall(
        tool_name="maltego_search",
        arguments={"goal": GOAL, "entity_type": "maltego.Domain", "entity_value": "example.org"},
        result={"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False},
        requested_at="2026-11-07T09:59:00Z",
        completed_at="2026-11-07T09:59:01Z",
        server=SERVER,
    )
    return build_record(call, SERVER_URL)


def describe_record(ids: list[str]) -> MaltegoLookupRecord:
    items = [
        {
            "id": capability,
            "kind": "transform",
            "name": capability,
            "input_types": ["maltego.Domain"],
            "output_types": {"effective": ["maltego.WHOISRecord"]},
            "settings_schema": {"type": "object", "properties": {}, "required": []},
        }
        for capability in ids
    ]
    call = ToolCall(
        tool_name="maltego_describe",
        arguments={"ids": ids},
        result={
            "content": [{"type": "text", "text": json.dumps({"items": items})}],
            "isError": False,
        },
        requested_at="2026-11-07T09:59:02Z",
        completed_at="2026-11-07T09:59:03Z",
        server=SERVER,
    )
    return build_record(call, SERVER_URL, {"purpose": "describe"})


def line_of(record: MaltegoLookupRecord, prefix: str) -> int:
    for line in index_lines(record.text):
        if line.text.startswith(prefix):
            return line.number
    raise AssertionError(f"no line starts with {prefix!r}")


def cite(record_label: str, record: MaltegoLookupRecord, prefix: str) -> dict:
    number = line_of(record, prefix)
    text = index_lines(record.text)[number - 1].text
    return {"record": record_label, "line": number, "quote": text}


def domain_draft(
    whois: MaltegoLookupRecord, *, extra_assumptions=(), claim_assumptions=()
) -> InvestigationDraft:
    """An honest investigator draft for example.org, plus optional extra assumptions."""
    return InvestigationDraft.model_validate(
        {
            "claims": [
                {
                    "id": "C1",
                    "statement": "example.org is registered through Corvid Registry, Inc. and was created on 2009-03-14.",
                    "steps": [
                        {
                            "id": "S1",
                            "statement": "The registrar of example.org is Corvid Registry, Inc.",
                            "citations": [cite("R1", whois, "registrar:")],
                            "assumption_ids": [],
                        },
                        {
                            "id": "S2",
                            "statement": "example.org was created on 2009-03-14.",
                            "citations": [cite("R1", whois, "creationDate:")],
                            "assumption_ids": ["A1"],
                        },
                    ],
                    "assumption_ids": [],
                },
                {
                    "id": "C2",
                    "statement": "Larkspur Holdings Ltd is the registrant organisation of example.org.",
                    "steps": [
                        {
                            "id": "S1",
                            "statement": "The WHOIS registrant organisation is Larkspur Holdings Ltd.",
                            "citations": [cite("R1", whois, "registrantOrganization:")],
                            "assumption_ids": [],
                        }
                    ],
                    "assumption_ids": list(claim_assumptions),
                },
            ],
            "assumptions": [
                {
                    "id": "A1",
                    "statement": "The WHOIS data reflects the current registration.",
                    "reason": "The lookup ran on 2026-11-07.",
                },
                *extra_assumptions,
            ],
            "gaps": ["The records do not show who operates the website."],
        }
    )


class FakeSource:
    """Stands in for MaltegoDataSource: returns fixture records and saves them."""

    def __init__(self, store: RecordStore):
        self.store = store
        self.calls: list[tuple[str, dict]] = []
        self.records = {"ci_TestWhois": whois_record(), "ci_TestWhoisHistory": history_record()}

    def search(self, goal, **arguments):
        self.calls.append(("search", {"goal": goal, **arguments}))
        record = search_record()
        self.store.save(record)
        return record

    def describe(self, ids):
        self.calls.append(("describe", {"ids": list(ids)}))
        record = describe_record(list(ids))
        self.store.save(record)
        return record

    def lookup(self, capability_id, **arguments):
        self.calls.append(("lookup", {"capability_id": capability_id, **arguments}))
        if arguments.get("validate_only"):
            return LookupOutcome((), "validated", None, "")
        record = self.records[capability_id]
        self.store.save(record)
        payload = record.payload() or {}
        return LookupOutcome(
            (record,), payload.get("status", "ok"), payload.get("run_id"), "traced-test"
        )


class FakeVerifier:
    """A Verifier that answers from a rule table, quoting passages verbatim."""

    model = "test/fake-verifier"
    provider = "fake"
    actual_model = "test/fake-verifier"

    def __init__(self, rules=None):
        self.rules = rules or []
        self.calls: list[tuple[str, list[dict]]] = []

    def check(self, statement, passages):
        self.calls.append((statement, passages))
        findings = []
        for needle, kind, quote_from, explanation in self.rules:
            if needle not in statement:
                continue
            for passage in passages:
                if quote_from in passage["text"]:
                    findings.append(
                        {
                            "finding_type": kind,
                            "explanation": explanation,
                            "fragment_ids": [passage["fragment_id"]],
                            "quotes": [quote_from],
                        }
                    )
                    break
        return {"findings": findings}


class FakeModel:
    """A StructuredModel that returns a fixed draft and records the prompt."""

    model = "test/fake-writer"
    provider = "fake"
    actual_model = "test/fake-writer"

    def __init__(self, draft: InvestigationDraft):
        self.draft = draft
        self.messages = None

    def complete(self, messages, schema, *, name, max_tokens):
        self.messages = messages
        return schema.model_validate(self.draft.model_dump(mode="json"))
