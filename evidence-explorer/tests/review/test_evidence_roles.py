"""Source cards preserve declared roles, exact evidence and existing navigation IDs."""

from copy import deepcopy

from dash import html
from dash.development.base_component import Component

from evidence_explorer.review.evidence_roles import evidence_source_cards
from evidence_explorer.review.workspace_views import finding_summary, linked_sources


def _bundle():
    records = [
        {
            "id": f"r{number}",
            "kind": "text",
            "source_label": f"Source {number}",
            "text": f"Saved source {number}. Second exact sentence.",
            "published_at": None,
            "metadata": {},
        }
        for number in range(1, 7)
    ]
    records[0].update(source_url="https://example.org/first", published_at="2026-11-06")
    records[0]["metadata"] = {"publisher": "Example publisher", "synthetic": False}
    records[3]["artifact_ref"] = "media/supplied.png"
    records[4]["metadata"]["synthetic"] = True
    records[5].update(text=records[0]["text"], source_url=records[0]["source_url"])
    fragments = [
        {
            "id": f"f{number}",
            "record_id": record["id"],
            "quote": record["text"],
            "selector": {"kind": "text", "start": 0, "end": len(record["text"])},
        }
        for number, record in enumerate(records, 1)
    ]

    def link(identity, fragment, relation, *, target="selected", **extras):
        return {
            "id": identity,
            "source": {"type": "fragment", "id": fragment},
            "target": {"type": "finding", "id": target},
            "relation": relation,
            "fragment_ids": [fragment],
            "basis": f"Saved basis for {identity}.",
            **extras,
        }

    return {
        "records": records,
        "fragments": fragments,
        "findings": [
            {
                "snapshot_id": "selected",
                "statement": "Selected claim",
                "details": {
                    "assessment": {
                        "metadata": {"challenge": {"findings": [{"fragment_ids": ["f3"]}]}}
                    }
                },
            },
            {"snapshot_id": "premise", "statement": "Linked finding"},
        ],
        "links": [
            link("support", "f1", "supports", assessment_citation_stance="supports"),
            link("challenge", "f1", "contradicts", assessment_citation_stance="contradicts"),
            link("context", "f1", "depends_on", assessment_citation_stance="context"),
            link("context-copy", "f1", "depends_on", assessment_citation_stance="context"),
            link("input", "f2", "depends_on", assessment_input=True, challenge_input=True),
            link("dependency", "f3", "depends_on"),
            link("media-support", "f4", "supports"),
            link("premise-support", "f5", "supports", target="premise"),
            {
                "id": "finding-premise",
                "source": {"type": "finding", "id": "premise"},
                "target": {"type": "finding", "id": "selected"},
                "relation": "depends_on",
                "basis": "A declared premise, not direct support from its source.",
            },
            {
                "id": "declared-repost",
                "source": {"type": "record", "id": "r2"},
                "target": {"type": "record", "id": "r1"},
                "relation": "documented_repost",
                "basis": "Supplied repost attribution; not independently verified.",
            },
        ],
    }


def _components(node):
    if isinstance(node, Component):
        yield node
        yield from _components(getattr(node, "children", None))
    elif isinstance(node, (list, tuple)):
        for child in node:
            yield from _components(child)


def _text(node):
    if isinstance(node, str):
        return node
    if isinstance(node, (list, tuple)):
        return " ".join(_text(child) for child in node)
    return _text(getattr(node, "children", ""))


def test_multiple_direct_roles_preserve_explicit_model_stances_and_all_exact_references():
    bundle = _bundle()
    before = deepcopy(bundle)
    cards = evidence_source_cards(bundle, "selected")
    source = next(card for card in cards if card["record"]["id"] == "r1")
    roles = {role["label"]: role for role in source["roles"]}

    assert set(roles) == {
        "Supports · model proposal",
        "Challenges · model proposal",
        "Context · model proposal",
    }
    assert set(roles["Context · model proposal"]["links"]) == {"context", "context-copy"}
    assert len(roles["Context · model proposal"]["passages"]) == 1
    assert roles["Context · model proposal"]["passages"][0]["exact"] is True
    assert (
        roles["Supports · model proposal"]["passages"][0]["quote"] == bundle["records"][0]["text"]
    )
    assert roles["Challenges · model proposal"]["bases"] == ["Saved basis for challenge."]
    assert source["publisher"] == "Example publisher"
    assert source["domain"] == "example.org"
    assert bundle == before


def test_inputs_and_indirect_premises_are_not_promoted_to_support_or_context():
    cards = {card["record"]["id"]: card for card in evidence_source_cards(_bundle(), "selected")}

    assert {role["label"] for role in cards["r2"]["roles"]} == {
        "Assessment input",
        "Automated challenge input",
    }
    assert {role["label"] for role in cards["r3"]["roles"]} == {
        "Input dependency",
        "Automated challenge input",
    }
    assert cards["r4"]["roles"][0]["label"] == "Supports"
    assert cards["r5"]["roles"][0]["label"] == "Via linked finding"
    assert cards["r5"]["direct"] is False
    assert cards["r5"]["roles"][0]["passages"] == []


def test_source_overlap_and_declared_links_remain_separate_from_evidence_roles():
    source = next(
        card
        for card in evidence_source_cards(_bundle(), "selected")
        if card["record"]["id"] == "r1"
    )
    relations = {relation["label"]: relation for relation in source["relationships"]}

    assert set(relations) == {
        "Same saved text",
        "Shared supplied URL",
        "Declared documented repost",
    }
    assert [record["id"] for record in relations["Same saved text"]["peers"]] == ["r6"]
    assert relations["Shared supplied URL"]["url"] == "https://example.org/first"
    assert relations["Declared documented repost"]["direction"] == "Source 2 → Source 1"
    assert relations["Declared documented repost"]["link_id"] == "declared-repost"
    assert "origin is unknown" in relations["Same saved text"]["basis"]


def test_missing_or_mismatched_fragments_are_never_labeled_exact():
    bundle = _bundle()
    bundle["fragments"][0]["selector"]["end"] -= 1
    bundle["links"][0]["fragment_ids"].append("missing-fragment")
    card = next(
        card for card in evidence_source_cards(bundle, "selected") if card["record"]["id"] == "r1"
    )
    support = next(role for role in card["roles"] if role["kind"] == "supports")

    assert {passage["id"] for passage in support["passages"]} == {"f1", "missing-fragment"}
    assert all(passage["exact"] is False for passage in support["passages"])
    assert next(p for p in support["passages"] if p["id"] == "missing-fragment")["quote"] is None


def test_cards_preserve_media_order_existing_buttons_selection_exclusions_and_disclosure():
    bundle = _bundle()
    before = deepcopy(bundle)
    panel = linked_sources(bundle, "selected", "r1", {"r2"})
    top_cards = [node for node in panel if isinstance(node, html.Article)]
    more = next(node for node in panel if isinstance(node, html.Details))
    buttons = [
        component
        for component in _components(panel)
        if isinstance(component, html.Button)
        and getattr(component, "id", {}).get("location", "").startswith("linked-")
        and not component.id["location"].startswith("linked-relation-")
    ]

    assert len(top_cards) == 3
    assert [button.id for button in buttons] == [
        {"type": "rv-record", "rid": rid, "location": f"linked-{index}"}
        for index, rid in enumerate(("r4", "r1", "r2", "r3", "r5"))
    ]
    assert "All linked records (5)" in _text(more)
    assert getattr(buttons[1], "aria-pressed") == "true"
    assert "is-selected" in buttons[1].className
    assert "Excluded in scenario" in _text(top_cards[2])
    assert "Unknown publication time" in _text(top_cards[0])
    assert "Published: 2026-11-06" in _text(top_cards[1])
    assert "Collected source; factual accuracy requires review" in _text(top_cards[1])
    assert "Record origin not specified" in _text(top_cards[2])
    assert "Synthetic record" in _text(more)
    assert "not a reviewer decision" in _text(panel)
    assert "Input dependency is not support" in _text(panel)
    assert "Connections between saved sources" in _text(panel)
    assert "Exact saved quotation" in _text(panel)
    assert bundle == before


def test_unlinked_empty_and_premise_basis_labels_stay_neutral():
    bundle = _bundle()
    summary = _text(finding_summary(bundle, bundle["findings"][0]))

    assert "Evidence basis" in summary
    assert "Linked findings" in summary
    assert "What supports this statement" not in summary
    assert evidence_source_cards(bundle, "missing") == []
    assert "No evidence records linked" in _text(linked_sources(bundle, "missing", None, set()))
