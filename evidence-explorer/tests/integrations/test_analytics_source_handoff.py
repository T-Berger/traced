"""Source cards stay navigable and preserve saved semantics through native handoff.

All prose here is constructed test data. A declared non-synthetic record tests
the origin-field contract; it is not evidence of a real collection or review.
Actual saved articles are checked by the separate artifact verification script.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from types import SimpleNamespace

import pytest
from dash import html
from dash.development.base_component import Component

from evidence_explorer.config import Settings
from evidence_explorer.review import web
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.controller import select_assessment, session_for_case
from evidence_explorer.review.evidence_roles import evidence_source_cards
from evidence_explorer.review.integrations.handoff_service import (
    load_handoff_context,
    prepare_dossier,
    prepare_export,
)
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.presentation import record_origin_label, source_inspector
from evidence_explorer.review.selectors import evidence_ids
from evidence_explorer.review.workflow import assessment_decisions
from evidence_explorer.review.workspace_views import linked_sources

FINDING = "source-cards:test-finding-v1"
RUN = "source-cards:test-run"


def workspace(path):
    settings = Settings(data_dir=path, ollama_url="http://127.0.0.1:1")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    return WorkspaceService(repository, settings, path / "artifacts")


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


def table_hashes(service):
    result = {}
    with sqlite3.connect(f"file:{service.settings.db_path}?mode=ro", uri=True) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        for (name,) in tables:
            cursor = connection.execute('SELECT * FROM "' + name.replace('"', '""') + '"')
            rows = sorted(
                json.dumps(list(row), default=lambda value: {"bytes": value.hex()})
                for row in cursor.fetchall()
            )
            payload = json.dumps(
                {"columns": [column[0] for column in cursor.description], "rows": rows}
            ).encode()
            result[name] = {"rows": len(rows), "sha256": hashlib.sha256(payload).hexdigest()}
    return result


def constructed_bundle():
    dual = "Test fixture: the preliminary count was 12. The revised count was 9."
    records = []
    for identifier, body, origin in [
        ("dual", dual, False),
        ("context", "Test fixture: this is the supplied background account.", True),
        ("objection", "Test fixture: the counting method changed between releases.", None),
        ("input", "Test fixture: an uncited method note was supplied to the model.", False),
        ("copy", dual, None),
    ]:
        metadata = {"fixture_kind": "constructed_origin_contract_test", "publisher": "Test desk"}
        if origin is not None:
            metadata["synthetic"] = origin
        records.append(
            {
                "id": identifier,
                "kind": "article",
                "source_label": "Constructed " + identifier,
                "text": body,
                "content_hash": hashlib.sha256(body.encode()).hexdigest(),
                "source_url": "https://example.test/" + identifier,
                "published_at": None,
                "acquired_at": "2026-11-07T09:00:00+00:00",
                "metadata": metadata,
            }
        )
    fragments = []
    for identifier, record_id, quote in [
        ("support", "dual", "the preliminary count was 12"),
        ("opposes", "dual", "The revised count was 9"),
        ("context", "context", records[1]["text"]),
        ("objection", "objection", records[2]["text"]),
        ("input", "input", records[3]["text"]),
    ]:
        body = next(record["text"] for record in records if record["id"] == record_id)
        start = body.index(quote)
        fragments.append(
            {
                "id": "fragment:" + identifier,
                "record_id": record_id,
                "selector": {"kind": "text", "start": start, "end": start + len(quote)},
                "quote": quote,
            }
        )
    citations = [
        {"fragment_id": fragment["id"], "quote": fragment["quote"], "stance": stance}
        for fragment, stance in zip(
            fragments[:3], ["supports", "contradicts", "context"], strict=True
        )
    ]
    citations.insert(1, {**citations[0], "stance": "context"})
    links = []
    for fragment in fragments:
        links.append(
            {
                "id": "input:" + fragment["id"],
                "source": {"type": "fragment", "id": fragment["id"]},
                "target": {"type": "finding", "id": FINDING},
                "relation": "depends_on",
                "fragment_ids": [fragment["id"]],
                "basis": "Constructed saved assessment input; this does not establish support.",
                "run_id": RUN,
                "assessment_input": True,
                "challenge_input": fragment["id"] == "fragment:objection",
            }
        )
    for index, citation in enumerate(citations):
        links.append(
            {
                "id": f"stance:{index}:" + citation["fragment_id"],
                "source": {"type": "fragment", "id": citation["fragment_id"]},
                "target": {"type": "finding", "id": FINDING},
                "relation": citation["stance"] if citation["stance"] != "context" else "depends_on",
                "fragment_ids": [citation["fragment_id"]],
                "basis": "Constructed saved proposal; reviewer judgment is separate.",
                "run_id": RUN,
                "assessment_citation_stance": citation["stance"],
                "assessment_input": citation["stance"] == "context",
            }
        )
    links.append(
        {
            "id": "saved-text-relationship",
            "source": {"type": "record", "id": "dual"},
            "target": {"type": "record", "id": "copy"},
            "relation": "same_content",
            "basis": "Identical saved test text; independence is not established.",
            "run_id": RUN,
        }
    )
    return EvidenceBundle.model_validate(
        {
            "case": {
                "id": "source-cards:constructed-case",
                "title": "Constructed mixed-origin navigation case",
                "question": "Can a reviewer open and retain the exact saved source?",
                "synthetic": True,
                "coverage": {"fixture_only": True},
            },
            "records": records,
            "fragments": fragments,
            "runs": [
                {
                    "id": RUN,
                    "input_fingerprint": "constructed-fixture",
                    "input_manifest": [record["id"] for record in records],
                    "method": "constructed_card_handoff_test",
                    "method_version": "1",
                    "created_at": "2026-11-07T09:00:00+00:00",
                }
            ],
            "findings": [
                {
                    "snapshot_id": FINDING,
                    "lineage_id": "source-cards:test-finding",
                    "version": 1,
                    "run_id": RUN,
                    "kind": "inference",
                    "statement": "Constructed test claim: the count is 12.",
                    "details": {
                        "domain": "claim_check",
                        "synthetic": True,
                        "assessment": {
                            "status": "insufficient_evidence",
                            "citations": citations,
                            "metadata": {
                                "challenge": {
                                    "status": "completed",
                                    "findings": [
                                        {
                                            "finding_type": "unsupported_leap",
                                            "fragment_ids": ["fragment:objection"],
                                            "quotes": [fragments[3]["quote"]],
                                            "explanation": "Constructed candidate objection.",
                                        }
                                    ],
                                }
                            },
                        },
                    },
                }
            ],
            "links": links,
        }
    )


@pytest.fixture
def desk(tmp_path):
    service = workspace(tmp_path / "sender")
    bundle = constructed_bundle()
    service.import_bundle(bundle.model_dump(mode="json"))
    baseline = service.baseline_scenario(bundle.case.id, FINDING)
    service.review(
        bundle.case.id,
        FINDING,
        baseline["id"],
        "qualify",
        "Constructed test decision: preserve the conflicting counts and missing context.",
    )
    scenario = service.start_scenario(bundle.case.id, FINDING)
    scenario = service.exclude(
        scenario["id"], "context", True, "Constructed exclusion test.", scenario["version"]
    )
    return service, bundle.case.id, scenario["id"]


def selected_state(service, case_id, scenario_id=None):
    state = session_for_case(service, case_id)
    if scenario_id:
        select_assessment(service, state, scenario_id)
    return state


def assert_inspectable(bundle, finding_id, record_id, excluded):
    record = next(row for row in bundle["records"] if row["id"] == record_id)
    inspector = source_inspector(bundle, record_id, finding_id, excluded)
    shown = text(inspector)
    origin = record_origin_label(record["metadata"].get("synthetic"))
    assert f"Record provenance: {origin}." in shown
    links = [node.href for node in components(inspector) if isinstance(node, html.A)]
    assert record["source_url"] in links
    marks = "".join(text(node) for node in components(inspector) if isinstance(node, html.Mark))
    _, fragment_ids = evidence_ids(bundle, finding_id)
    for fragment in bundle["fragments"]:
        if fragment["record_id"] == record_id and fragment["id"] in fragment_ids:
            selector = fragment["selector"]
            assert record["text"][selector["start"] : selector["end"]] == fragment["quote"]
            assert fragment["quote"] in marks


def assert_native_projection(before, after, bundle):
    """Only the declared local-history-to-native identity conversion may differ."""
    selected = before["assessment"]
    native_id = f"review:{selected['id']}:v{selected['version']}"
    assert after["assessment"]["id"] == native_id
    assert before["assessment"]["imported"] is False
    assert after["assessment"]["imported"] is True
    if selected["is_baseline"]:
        assert after["assessment"]["label"] == "Baseline"
    else:
        scenarios = [row for row in bundle.scenarios if row.assessment_kind != "baseline"]
        number = next(i for i, row in enumerate(scenarios, 1) if row.id == native_id)
        assert after["assessment"]["label"] == f"Imported assessment {number}"
    normalized = deepcopy(after)
    for key in ("id", "imported", "label"):
        normalized["assessment"][key] = selected[key]
    normalized["assessment_label"] = before["assessment_label"]
    for old, restored in zip(before["included"], normalized["included"], strict=True):
        review = old["review"]
        native = next(row for row in bundle.reviews if row.id == review["id"])
        assert restored["review"] == {
            **native.model_dump(mode="json"),
            "finding_id": native.finding_snapshot_id,
            "created_at": native.timestamp,
            "imported": True,
        }
        for key in ("id", "reviewer", "disposition", "reason", "case_fingerprint"):
            assert getattr(native, key) == review[key]
        assert native.timestamp == review["created_at"]
        assert native.finding_snapshot_id == review["finding_id"]
        assert native.scenario_id == native_id
        restored["review"] = review
    assert normalized == before


def native_header(content, before, after):
    """Native imported labels can change only the visible assessment header."""
    old = f"Assessment: **{before['assessment']['label']}**"
    new = f"Assessment: **{after['assessment']['label']}**"
    assert content.count(new) == 1
    return content.replace(new, old, 1)


def test_card_buttons_open_exact_sources_without_losing_review_draft(desk, monkeypatch):
    service, case_id, scenario_id = desk
    state = selected_state(service, case_id, scenario_id)
    view = service.view(case_id, scenario_id)
    excluded = set(view["impact"]["excluded_record_ids"])
    before = table_hashes(service)
    cards = linked_sources(view["bundle"], FINDING, state["record_id"], excluded)
    buttons = [
        node
        for node in components(cards)
        if isinstance(getattr(node, "id", None), dict) and node.id.get("type") == "rv-record"
    ]
    direct, _ = evidence_ids(view["bundle"], FINDING)
    assert direct <= {button.id["rid"] for button in buttons}
    assert len({json.dumps(button.id, sort_keys=True) for button in buttons}) == len(buttons)
    app = web.create_app(service)
    definition = next(
        value for key, value in app.callback_map.items() if "rv-action-message.children" in key
    )
    note = "Unsaved test note: check the revised count before deciding."
    for index, button in enumerate(buttons):
        trigger = button.id
        intent = {"event_id": f"source-handoff-{index}", "trigger": trigger, "clicks": 1}
        monkeypatch.setattr(
            web,
            "ctx",
            SimpleNamespace(
                triggered_id="rv-click-intent",
                triggered=[{"prop_id": "rv-click-intent.data", "value": intent}],
            ),
        )
        values = {
            "rv-click-intent": intent,
            "rv-session": state,
            "rv-case": case_id,
            "rv-scenario": scenario_id,
            "rv-review-reason": note,
            "rv-exclude-reason": "",
            "rv-challenge-mode": "demo",
        }
        args = []
        for field in [*definition["inputs"], *definition["state"]]:
            identifier = field["id"]
            if identifier.startswith("{"):
                args.append([0])
            else:
                args.append(values.get(identifier, 0))
        result = definition["callback"].__wrapped__(*args)
        assert result[0]["record_id"] == trigger["rid"]
        assert result[0]["finding_id"] == FINDING
        assert result[0]["scenario_id"] == scenario_id
        # Navigation never writes or clears the separate reviewer-note input.
        assert all(output.component_id != "rv-review-reason" for output in definition["output"])
        assert_inspectable(view["bundle"], FINDING, trigger["rid"], excluded)
    assert table_hashes(service) == before


@pytest.mark.parametrize("selection", ["baseline", "excluded-scenario"])
def test_mixed_source_roles_and_decisions_survive_export_and_reopen(desk, tmp_path, selection):
    service, case_id, scenario_id = desk
    state = selected_state(service, case_id, None if selection == "baseline" else scenario_id)
    before = table_hashes(service)
    original = service.export_bundle(case_id)
    view, handoff, _ = load_handoff_context(service, state)
    decisions = assessment_decisions(view)
    assert bool(decisions) is (selection == "baseline")
    cards = evidence_source_cards(view["bundle"], FINDING)
    roles = {card["record"]["id"]: {role["kind"] for role in card["roles"]} for card in cards}
    assert {"supports", "contradicts", "context"} <= roles["dual"]
    assert "context" in roles["context"] and "supports" not in roles["context"]
    assert roles["input"] == {"input"}
    if selection == "baseline":
        citation = next(
            row
            for row in handoff["brief"]["included"][0]["evidence"]
            if row["fragment_id"] == "fragment:support"
        )
        assert {"supports", "context"} <= set(citation["evidence_roles"])
    assert {record.metadata.get("synthetic") for record in original.records} == {True, False, None}
    exports = {
        kind: prepare_export(service, kind, state, expected_context=handoff["download_context"])
        for kind in ("package", "json", "brief")
    }
    dossier = prepare_dossier(service, state, expected_context=handoff["download_context"])
    assert EvidenceBundle.model_validate_json(exports["json"].content) == original
    assert table_hashes(service) == before
    recipient = workspace(tmp_path / "recipient")
    assert recipient.import_package(exports["package"].content) == case_id
    assert recipient.export_bundle(case_id) == original
    recipient_before = table_hashes(recipient)
    if selection == "baseline":
        restored = selected_state(recipient, case_id)
    else:
        excluded = next(row for row in original.scenarios if row.excluded_record_ids == ["context"])
        restored = selected_state(recipient, case_id, excluded.id)
    restored_view, reopened, _ = load_handoff_context(recipient, restored)
    assert evidence_source_cards(restored_view["bundle"], FINDING) == cards
    assert assessment_decisions(restored_view).keys() == decisions.keys()
    assert_native_projection(handoff["brief"], reopened["brief"], original)
    reopened_brief = prepare_export(
        recipient, "brief", restored, expected_context=reopened["download_context"]
    ).content
    assert (
        native_header(reopened_brief, handoff["brief"], reopened["brief"])
        == exports["brief"].content
    )
    reopened_dossier = prepare_dossier(
        recipient, restored, expected_context=reopened["download_context"]
    ).content
    assert native_header(reopened_dossier, handoff["brief"], reopened["brief"]) == dossier.content
    excluded_ids = restored_view["impact"]["excluded_record_ids"]
    for record in original.records:
        assert_inspectable(restored_view["bundle"], FINDING, record.id, excluded_ids)
    assert recipient.repository.list_events(case_id) == []
    assert recipient.planning_briefs.list() == ()
    assert table_hashes(recipient) == recipient_before
    assert table_hashes(service) == before
