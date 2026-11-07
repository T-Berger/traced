"""Source connections show a focused, verifiable path without arbitrary outlet navigation."""

from __future__ import annotations

import json
from copy import deepcopy

from evidence_explorer.graphs import source_graph
from evidence_explorer.ui import graph_elements


def source_material():
    articles = [
        {
            "id": index,
            "title": f"Saved article {index}",
            "publisher": "Shared outlet",
            "text": " ".join(f"Exact quoted passage {index}-{part}." for part in range(8))
            + " Saved surrounding context remains available in the article inspector.",
        }
        for index in range(1, 11)
    ]
    claims = [
        {
            "id": f"claim-{part:02d}",
            "article_id": 2,
            "claim_text": f"Statement {part} derived from the selected saved article.",
            "quote": f"Exact quoted passage 2-{part}.",
            "source_verified": True,
        }
        for part in range(8)
    ]
    claims += [
        {
            "id": "invalid-but-marked-verified",
            "article_id": 2,
            "claim_text": "This claim has a mismatched quotation.",
            "quote": "Words that never appeared in the selected saved article.",
            "source_verified": True,
        },
        {
            "id": "other-article-claim",
            "article_id": 1,
            "claim_text": "A statement about another article.",
            "quote": "Exact quoted passage 1-0.",
            "source_verified": True,
        },
    ]
    return articles, claims


def _nodes_and_edges(elements):
    nodes = {
        element["data"]["id"]: element for element in elements if "source" not in element["data"]
    }
    edges = [element["data"] for element in elements if "source" in element["data"]]
    return nodes, edges


def test_source_graph_focuses_exact_article_and_reports_omitted_valid_claims():
    articles, claims = source_material()
    view = source_graph(articles, claims, selected_article_id=2, limit=5)
    nodes, edges = _nodes_and_edges(view.elements)
    assert view.article["id"] == 2
    assert len(nodes) <= 5
    assert len(view.claims) == 3
    assert view.omitted_claim_count == 5
    assert "3" in view.summary and "8" in view.summary
    assert {claim["article_id"] for claim in view.claims} == {2}
    assert all(claim["quote"] in articles[1]["text"] for claim in view.claims)
    assert "invalid-but-marked-verified" not in {
        element["data"].get("claim_id") for element in nodes.values()
    }
    assert "other-article-claim" not in {claim["id"] for claim in view.claims}
    assert articles[1]["text"] not in json.dumps(view.elements)
    for edge in edges:
        assert edge["source"] in nodes and edge["target"] in nodes


def test_publisher_node_has_no_arbitrary_article_and_arrows_follow_the_evidence_path():
    articles, claims = source_material()
    view = source_graph(articles, claims, selected_article_id=2)
    nodes, edges = _nodes_and_edges(view.elements)
    publishers = [
        element["data"] for element in nodes.values() if element["data"]["kind"] == "publisher"
    ]
    assert len(publishers) == 1
    assert "article_id" not in publishers[0]
    for element in nodes.values():
        data = element["data"]
        if data["kind"] in {"article", "claim"}:
            assert data["article_id"] == 2
        if data["kind"] == "claim":
            assert data["claim_id"] in {claim["id"] for claim in view.claims}
    kinds = {
        (nodes[edge["source"]]["data"]["kind"], nodes[edge["target"]]["data"]["kind"])
        for edge in edges
    }
    assert kinds == {("publisher", "article"), ("article", "claim")}


def test_claim_ordering_and_positions_are_stable_when_inputs_are_reordered():
    articles, claims = source_material()
    original = source_graph(articles, claims, selected_article_id=2)
    alternate = source_graph(
        list(reversed(articles)), list(reversed(claims)), selected_article_id=2
    )
    assert alternate.elements == original.elements
    assert alternate.claims == original.claims
    assert alternate.omitted_claim_count == original.omitted_claim_count


def test_cached_verification_is_used_only_when_the_saved_body_was_not_requested():
    articles, claims = source_material()
    selected = deepcopy(articles[1])
    selected.pop("text")
    valid = claims[0]
    not_verified = {**claims[1], "source_verified": False}
    summary = source_graph([selected], [valid, not_verified], selected_article_id=2)
    assert {claim["id"] for claim in summary.claims} == {valid["id"]}
    empty_body = source_graph([{**selected, "text": ""}], [valid], selected_article_id=2)
    assert not empty_body.claims


def test_legacy_elements_wrapper_uses_the_same_bounded_graph():
    articles, claims = source_material()
    assert (
        graph_elements(articles, claims, limit=5)
        == source_graph(articles, claims, limit=5).elements
    )
    empty = source_graph([], [], selected_article_id=None)
    assert empty.elements == []
    assert not empty.claims
