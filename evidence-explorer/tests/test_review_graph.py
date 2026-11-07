"""Bounded evidence diagrams preserve typed references and every collapsed citation."""

from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest

from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.graph import build_evidence_graph, direct_connections
from evidence_explorer.review.presentation import evidence_graph
from evidence_explorer.review.service import ReviewService
from evidence_explorer.review.storage import ReviewRepository
from evidence_explorer.review.web import create_app


def graph_bundle(record_count=1, fragments_per_record=2):
    records, fragments, links = [], [], []
    for index in range(record_count):
        record_id = f"record-{index:03d}"
        first = f"First exact saved passage for record {index}."
        second = f"Second exact saved passage for record {index}."
        text = first + " " + second
        records.append(
            {
                "id": record_id,
                "kind": "text",
                "source_label": f"Saved source {index}",
                "text": text,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            }
        )
        for citation in range(fragments_per_record):
            quote, start = (first, 0) if citation % 2 == 0 else (second, len(first) + 1)
            fragment_id = f"fragment-{index:03d}-{citation:03d}"
            fragments.append(
                {
                    "id": fragment_id,
                    "record_id": record_id,
                    "selector": {"kind": "text", "start": start, "end": start + len(quote)},
                    "quote": quote,
                }
            )
            links.append(
                {
                    "id": f"link-{index:03d}-{citation:03d}",
                    "source": {"type": "fragment", "id": fragment_id},
                    "target": {"type": "finding", "id": "selected"},
                    "relation": "supports",
                    "fragment_ids": [fragment_id],
                    "basis": f"Saved citation {citation} from record {index}.",
                    "run_id": "run",
                }
            )
    data = {
        "case": {
            "id": "graph-case",
            "title": "Synthetic graph review",
            "question": "What do the saved records support?",
            "synthetic": True,
        },
        "records": records,
        "fragments": fragments,
        "runs": [
            {
                "id": "run",
                "input_fingerprint": "saved-input",
                "input_manifest": [r["id"] for r in records],
                "method": "synthetic_fixture",
                "method_version": "1",
                "created_at": "2026-11-06T10:00:00Z",
            }
        ],
        "findings": [
            {
                "snapshot_id": "selected",
                "lineage_id": "selected-lineage",
                "version": 1,
                "run_id": "run",
                "kind": "inference",
                "statement": "A selected inference whose full wording remains inspectable.",
            }
        ],
        "links": links,
    }
    return EvidenceBundle.model_validate(data).model_dump(mode="json")


def _parts(view):
    nodes = {
        element["data"]["id"]: element
        for element in view.elements
        if "source" not in element["data"]
    }
    edges = [element for element in view.elements if "source" in element["data"]]
    return nodes, edges


def test_repeated_fragments_collapse_without_losing_link_or_quote_provenance():
    bundle = graph_bundle(fragments_per_record=40)
    view = build_evidence_graph(bundle, "selected")
    nodes, edges = _parts(view)
    assert len(nodes) == 2
    assert len(edges) == view.relationship_count == 1
    assert view.shown_premises == view.total_premises == 1
    assert view.shown_links == view.total_links == 40
    data = edges[0]["data"]
    assert data["kind"] == "evidence_link"
    assert data["source_kind"] == "record"
    assert data["source_ref"] == "record-000"
    assert data["target_ref"] == "selected"
    assert set(data["link_ids"]) == {link["id"] for link in bundle["links"]}
    assert set(data["fragment_ids"]) == {fragment["id"] for fragment in bundle["fragments"]}
    assert set(data["bases"]) == {link["basis"] for link in bundle["links"]}
    assert data["source"] == "record:record-000"
    assert data["target"] == "finding:selected"


def test_preview_bounds_and_summary_report_all_hidden_premises():
    bundle = graph_bundle(record_count=12)
    view = build_evidence_graph(bundle, "selected", max_premises=4)
    nodes, edges = _parts(view)
    assert len(nodes) == 5
    assert len(edges) == view.relationship_count == 4
    assert view.shown_premises == 4
    assert view.total_premises == 12
    assert view.hidden_premises == 8
    assert view.shown_links == 8
    assert view.total_links == 24
    assert "4" in view.summary and "12" in view.summary
    assert "8" in view.summary and "24" in view.summary
    assert "all" in view.summary.lower()
    for edge in edges:
        assert edge["data"]["source"] in nodes
        assert edge["data"]["target"] in nodes
    assert evidence_graph(bundle, "selected", set(), set()) == view.elements


def test_open_record_stays_visible_and_input_permutation_does_not_move_the_graph():
    bundle = graph_bundle(record_count=12, fragments_per_record=3)
    view = build_evidence_graph(bundle, "selected", active_record_id="record-011")
    nodes, _ = _parts(view)
    assert "record:record-011" in nodes
    assert "focused" in nodes["record:record-011"]["classes"]
    assert len(nodes) == 5
    alternate = deepcopy(bundle)
    for field in ("records", "findings", "fragments", "links"):
        alternate[field].reverse()
    assert (
        build_evidence_graph(alternate, "selected", active_record_id="record-011").elements
        == view.elements
    )


def test_typed_ids_and_relationships_do_not_transfer_record_exclusions_to_findings():
    bundle = graph_bundle()
    record_id = bundle["records"][0]["id"]
    premise = {
        **bundle["findings"][0],
        "snapshot_id": record_id,
        "lineage_id": "premise-lineage",
        "statement": "An independently reviewed premise finding.",
    }
    bundle["findings"].append(premise)
    bundle["links"].append(
        {
            "id": "premise-finding-link",
            "source": {"type": "finding", "id": record_id},
            "target": {"type": "finding", "id": "selected"},
            "relation": "depends_on",
            "fragment_ids": [],
            "basis": "Declared premise dependency.",
            "run_id": "run",
        }
    )
    bundle["links"].append(
        {
            "id": "counterevidence-link",
            "source": {"type": "fragment", "id": bundle["fragments"][0]["id"]},
            "target": {"type": "finding", "id": "selected"},
            "relation": "contradicts",
            "fragment_ids": [],
            "basis": "The supplied passage also challenges this wording.",
            "run_id": "run",
        }
    )
    EvidenceBundle.model_validate(bundle)
    view = build_evidence_graph(bundle, "selected", {"selected", record_id}, {record_id})
    nodes, edges = _parts(view)
    assert f"record:{record_id}" in nodes
    assert f"finding:{record_id}" in nodes
    assert "excluded" in nodes[f"record:{record_id}"]["classes"]
    assert "Excluded record" in nodes[f"record:{record_id}"]["data"]["label"]
    assert "excluded" not in nodes[f"finding:{record_id}"]["classes"]
    assert "affected" in nodes["finding:selected"]["classes"]
    assert "affected" in nodes[f"finding:{record_id}"]["classes"]
    assert "needs review" in nodes["finding:selected"]["data"]["status_label"].lower()
    assert "needs review" in nodes[f"finding:{record_id}"]["data"]["status_label"].lower()
    assert "Reviewed finding" not in nodes["finding:selected"]["data"]["label"]
    relations = {edge["data"]["relation"] for edge in edges}
    assert relations == {"supports", "contradicts", "depends_on"}
    for edge in edges:
        data = edge["data"]
        assert data["target"] == "finding:selected"
        assert data["source"] == f"{data['source_kind']}:{data['source_ref']}"
        assert ("excluded" in edge.get("classes", "")) == (data["source_kind"] == "record")
        expected_label = (
            "premise for"
            if data["relation"] == "depends_on"
            else data["relation"].replace("_", " ")
        )
        assert data["label"] == expected_label


def test_dangling_incoming_references_are_not_counted_as_visible_evidence():
    bundle = graph_bundle()
    expected = build_evidence_graph(bundle, "selected")
    template = bundle["links"][0]
    bundle["links"] += [
        {**template, "id": "missing-record", "source": {"type": "record", "id": "absent"}},
        {**template, "id": "missing-fragment", "source": {"type": "fragment", "id": "absent"}},
    ]
    actual = build_evidence_graph(bundle, "selected")
    assert actual == expected
    assert build_evidence_graph(bundle, "absent").elements == []
    with pytest.raises(ValueError):
        build_evidence_graph(bundle, "selected", max_premises=0)


@pytest.mark.parametrize(
    ("flags", "label"),
    [
        ({}, "input dependency"),
        ({"assessment_input": True}, "assessment input"),
        ({"challenge_input": True}, "challenge input"),
        ({"assessment_input": True, "challenge_input": True}, "assessment / challenge input"),
        ({"assessment_input": "true", "challenge_input": 1}, "input dependency"),
    ],
)
def test_input_dependency_label_does_not_imply_support_or_change_evidence(flags, label):
    bundle = graph_bundle()
    for link in bundle["links"]:
        link["relation"] = "depends_on"
    before = direct_connections(bundle, "selected")[0]
    for link in bundle["links"]:
        link.update(flags)
    edge = direct_connections(bundle, "selected")[0]
    assert edge["label"] == label
    assert {key: value for key, value in edge.items() if key != "label"} == {
        key: value for key, value in before.items() if key != "label"
    }
    assert edge["fragment_ids"] == [fragment["id"] for fragment in bundle["fragments"]]
    assert edge["link_ids"] == [link["id"] for link in bundle["links"]]


def test_grouped_input_and_opposing_stances_keep_separate_connections():
    bundle = graph_bundle(fragments_per_record=3)
    bundle["links"][0].update(relation="depends_on", assessment_input=True)
    bundle["links"][1].update(relation="depends_on", challenge_input=True)
    template = bundle["links"][2]
    bundle["links"].append({**template, "id": "opposing", "relation": "contradicts"})
    original = deepcopy(bundle)
    edges = {edge["relation"]: edge for edge in direct_connections(bundle, "selected")}
    assert set(edges) == {"depends_on", "supports", "contradicts"}
    assert edges["depends_on"]["label"] == "assessment / challenge input"
    assert len(edges["depends_on"]["fragment_ids"]) == 2
    assert edges["supports"]["fragment_ids"] == edges["contradicts"]["fragment_ids"]
    assert edges["supports"]["id"] != edges["contradicts"]["id"]
    assert bundle == original


def _post_callback(app, output_fragment, changed_property, values):
    key = next(key for key in app.callback_map if output_fragment in key)
    definition = app.callback_map[key]
    declared = definition["output"]
    outputs = (
        [{"id": item.component_id, "property": item.component_property} for item in declared]
        if isinstance(declared, list)
        else {"id": declared.component_id, "property": declared.component_property}
    )
    inputs = [
        {
            **item,
            "value": values.get(
                item["id"] + "." + item["property"],
                [] if item["id"].startswith("{") else None,
            ),
        }
        for item in definition["inputs"]
    ]
    response = app.server.test_client().post(
        "/_dash-update-component",
        json={
            "output": key,
            "outputs": outputs,
            "inputs": inputs,
            "state": [
                {**item, "value": values.get(item["id"] + "." + item["property"])}
                for item in definition["state"]
            ],
            "changedPropIds": [changed_property],
        },
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["response"]


def _wire_components(value):
    if isinstance(value, dict) and "props" in value:
        yield value
        yield from _wire_components(value["props"].get("children"))
    elif isinstance(value, list):
        for child in value:
            yield from _wire_components(child)


@pytest.fixture
def graph_desk(tmp_path):
    bundle = graph_bundle(record_count=2)
    bundle["links"][1]["relation"] = "contradicts"
    bundle["findings"].append(
        {
            **bundle["findings"][0],
            "snapshot_id": "premise",
            "lineage_id": "premise-lineage",
            "statement": "A supporting finding with its own exact source.",
        }
    )
    bundle["links"] += [
        {
            "id": "premise-to-selected",
            "source": {"type": "finding", "id": "premise"},
            "target": {"type": "finding", "id": "selected"},
            "relation": "depends_on",
            "fragment_ids": [],
            "basis": "The inference depends on this finding.",
            "run_id": "run",
        },
        {
            "id": "record-to-premise",
            "source": {"type": "fragment", "id": "fragment-001-000"},
            "target": {"type": "finding", "id": "premise"},
            "relation": "supports",
            "fragment_ids": [],
            "basis": "Exact evidence for the premise.",
            "run_id": "run",
        },
    ]
    repository = ReviewRepository(tmp_path / "graph.sqlite3")
    repository.initialize()
    service = ReviewService(repository)
    case_id = service.import_bundle(bundle)
    state = session_for_case(service, case_id)
    assert state["finding_id"] == "selected"
    return service, create_app(service), state, bundle


def test_clicked_connection_opens_its_canonical_record_and_exact_cited_span(graph_desk):
    service, app, state, bundle = graph_desk
    model = build_evidence_graph(bundle, "selected")
    _, edges = _parts(model)
    edge = next(
        element["data"] for element in edges if element["data"]["relation"] == "contradicts"
    )
    # Browser-supplied display data cannot replace the server's saved connection identity.
    supplied = {**edge, "source_ref": "record-001", "fragment_ids": ["fragment-001-000"]}
    values = {
        "rv-session.data": state,
        "rv-case.value": state["case_id"],
        "rv-scenario.value": "baseline",
        "rv-graph.tapEdgeData": supplied,
    }
    selected = _post_callback(app, "rv-action-message.children", "rv-graph.tapEdgeData", values)[
        "rv-session"
    ]["data"]
    assert selected["finding_id"] == "selected"
    assert selected["record_id"] == "record-000"
    assert selected["graph_edge_id"] == edge["id"]
    assert selected["focus_fragment_ids"] == ["fragment-000-001"]
    rendered = _post_callback(
        app, "rv-source.children", "rv-session.data", {"rv-session.data": selected}
    )
    marks = [
        component["props"]["children"]
        for component in _wire_components(rendered["rv-source"]["children"])
        if component["type"] == "Mark"
    ]
    assert marks == [bundle["fragments"][1]["quote"]]
    assert service.repository.list_events(state["case_id"]) == []


def test_clicked_record_or_premise_node_clears_the_previous_edge_focus(graph_desk):
    _, app, state, bundle = graph_desk
    model = build_evidence_graph(bundle, "selected")
    nodes, _ = _parts(model)
    state = {
        **state,
        "graph_edge_id": "previous-connection",
        "focus_fragment_ids": ["fragment-000-001"],
    }
    record = _post_callback(
        app,
        "rv-action-message.children",
        "rv-graph.tapNodeData",
        {
            "rv-session.data": state,
            "rv-case.value": state["case_id"],
            "rv-graph.tapNodeData": nodes["record:record-001"]["data"],
        },
    )["rv-session"]["data"]
    assert record["record_id"] == "record-001"
    assert record["finding_id"] == "selected"
    assert "focus_fragment_ids" not in record
    assert "graph_edge_id" not in record
    premise = _post_callback(
        app,
        "rv-action-message.children",
        "rv-graph.tapNodeData",
        {
            "rv-session.data": record,
            "rv-case.value": record["case_id"],
            "rv-graph.tapNodeData": nodes["finding:premise"]["data"],
        },
    )["rv-session"]["data"]
    assert premise["finding_id"] == "premise"
    assert premise["record_id"] == "record-001"
    rendered = _post_callback(
        app, "rv-source.children", "rv-session.data", {"rv-session.data": premise}
    )
    marks = [
        component["props"]["children"]
        for component in _wire_components(rendered["rv-source"]["children"])
        if component["type"] == "Mark"
    ]
    assert marks == [bundle["fragments"][2]["quote"]]


def test_stale_connection_id_reports_error_without_changing_selection(graph_desk):
    service, app, state, _ = graph_desk
    result = _post_callback(
        app,
        "rv-action-message.children",
        "rv-graph.tapEdgeData",
        {
            "rv-session.data": state,
            "rv-case.value": state["case_id"],
            "rv-graph.tapEdgeData": {"id": "connection-from-another-case"},
        },
    )
    assert "rv-session" not in result
    assert any(
        component["props"].get("role") == "alert"
        for component in _wire_components(result["rv-action-message"]["children"])
    )
    assert service.repository.list_events(state["case_id"]) == []
