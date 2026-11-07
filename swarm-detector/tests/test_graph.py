"""A bounded connection preview must retain complete, inspectable pair evidence."""

from __future__ import annotations

import math
from copy import deepcopy
from itertools import combinations
from types import SimpleNamespace

import pytest

from swarm_detector.graph import graph_pair_rows, graph_view
from swarm_detector.presentation import episode_options, pair_options, with_selected_pair
from swarm_detector.service import Workbench
from swarm_detector.ui import graph_elements


@pytest.fixture
def crowded_case():
    accounts = [f"shared-account-identifier-prefix-{index:03d}" for index in range(45)]
    pairs = [
        {
            "pair_key": f"pair-{left:03d}-{right:03d}",
            "account_a": accounts[left],
            "account_b": accounts[right],
            "retained": (left + right) % 3 != 0,
            "observed": 12,
            "null_mean": 2.5,
            "null_p95": 5,
            "events": [],
        }
        for left, right in combinations(range(len(accounts)), 2)
    ]
    return {
        "case": {"case_id": "crowded", "account_keys": accounts, "peripheral_keys": []},
        "run": {"run_id": "saved-run"},
        "accounts": [{"account_key": account} for account in accounts],
        "pairs": pairs,
        "pagination": {
            "accounts": {"total": 80, "shown": len(accounts), "offset": 0},
            "pairs": {"total": 1200, "shown": len(pairs), "offset": 0},
        },
    }


def _parts(view):
    nodes = [element for element in view["elements"] if "source" not in element["data"]]
    edges = [element for element in view["elements"] if "source" in element["data"]]
    return nodes, edges


def test_dense_graph_is_bounded_and_honest_about_available_coverage(crowded_case):
    view = graph_view(crowded_case)
    nodes, edges = _parts(view)
    assert 0 < len(nodes) <= 10
    assert 0 < len(edges) <= 14
    assert view["account_count"] == len(nodes)
    assert view["pair_count"] == len(edges)
    assert view["loaded_account_count"] == 45
    assert view["loaded_pair_count"] == 990
    assert view["total_account_count"] == 80
    assert view["total_pair_count"] == 1200
    assert graph_elements(crowded_case) == view["elements"]
    ids = {node["data"]["id"] for node in nodes}
    pairs = {pair["pair_key"]: pair for pair in crowded_case["pairs"]}
    assert len(ids) == len(nodes)
    assert set(view["shown_pair_keys"]) == {edge["data"]["pair_key"] for edge in edges}
    for edge in edges:
        data = edge["data"]
        assert {data["source"], data["target"]} <= ids
        original = pairs[data["pair_key"]]
        assert {data["source"], data["target"]} == {original["account_a"], original["account_b"]}
        assert ("retained" in edge.get("classes", "")) == original["retained"]


def test_selected_pair_survives_graph_limits_and_common_account_prefixes(crowded_case):
    selected = crowded_case["pairs"][-1]
    view = graph_view(crowded_case, selected["pair_key"])
    nodes, edges = _parts(view)
    assert view["selected_pair_key"] == selected["pair_key"]
    assert selected["pair_key"] in view["shown_pair_keys"]
    ids = {node["data"]["id"] for node in nodes}
    assert {selected["account_a"], selected["account_b"]} <= ids
    labels = [node["data"]["label"] for node in nodes]
    assert len(labels) == len(set(labels)), (
        "Similar account IDs need distinguishable display labels"
    )
    positions = {(node["position"]["x"], node["position"]["y"]) for node in nodes}
    assert len(positions) == len(nodes)
    assert all(math.isfinite(value) for position in positions for value in position)
    assert len(nodes) <= 10 and len(edges) <= 14


def test_pair_ranking_and_graph_positions_are_deterministic_on_ties(crowded_case):
    selected = crowded_case["pairs"][-1]["pair_key"]
    original = graph_view(crowded_case, selected)
    reordered = deepcopy(crowded_case)
    reordered["pairs"].reverse()
    reordered["accounts"].reverse()
    reordered["case"]["account_keys"].reverse()
    alternate = graph_view(reordered, selected)
    assert alternate["shown_pair_keys"] == original["shown_pair_keys"]
    assert alternate["elements"] == original["elements"]
    assert pair_options(reordered) == pair_options(crowded_case)


def test_pairs_omitted_from_the_graph_remain_available_in_the_pair_selector(crowded_case):
    view = graph_view(crowded_case)
    options = pair_options(crowded_case)
    all_keys = {pair["pair_key"] for pair in crowded_case["pairs"]}
    selectable = {option["value"] for option in options}
    assert selectable == all_keys
    assert all_keys - set(view["shown_pair_keys"])
    for pair in crowded_case["pairs"]:
        option = next(option for option in options if option["value"] == pair["pair_key"])
        assert pair["account_a"] in option["label"]
        assert pair["account_b"] in option["label"]
        assert str(pair["observed"]) in option["label"]


def test_graph_detail_table_preserves_the_visible_pairs_exact_control_values(crowded_case):
    selected = crowded_case["pairs"][-1]["pair_key"]
    view = graph_view(crowded_case, selected)
    rows = graph_pair_rows(crowded_case, view)
    originals = {pair["pair_key"]: pair for pair in crowded_case["pairs"]}
    assert {row["pair_key"] for row in rows} == set(view["shown_pair_keys"])
    assert len(rows) == view["pair_count"]
    for row in rows:
        original = originals[row["pair_key"]]
        for field in ("account_a", "account_b", "observed", "null_mean", "null_p95", "retained"):
            assert row[field] == original[field]
        assert row["selected"] == (row["pair_key"] == selected)


def test_clicked_graph_pair_opens_its_saved_pair_events(tmp_path, monkeypatch):
    from swarm_detector import web

    monkeypatch.delenv("SWARM_BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("SWARM_BASIC_AUTH_PASSWORD", raising=False)
    workbench = Workbench(tmp_path / "graph.db")
    workbench.ensure_sample()
    case = workbench.list_cases(kind="candidate")[0]
    detail = workbench.case_detail(case["case_id"], limit=2)
    pair = detail["pairs"][-1]
    view = graph_view(detail, pair["pair_key"])
    edge = next(
        element["data"]
        for element in view["elements"]
        if element["data"].get("pair_key") == pair["pair_key"]
    )
    app = web.create_app(workbench)
    select = next(
        definition["callback"].__wrapped__
        for definition in app.callback_map.values()
        if definition.get("callback")
        and definition["callback"].__wrapped__.__name__ == "select_pair"
    )
    monkeypatch.setattr(web, "ctx", SimpleNamespace(triggered_id="pair-graph"))
    options, selected = select(detail, edge, None)
    assert selected == pair["pair_key"]
    assert selected in {option["value"] for option in options}
    load = app.callback_map["selected-pair-store.data"]["callback"].__wrapped__
    selection = load(selected, detail)
    expected = workbench.pair_events(case["case_id"], selected, run_id=detail["run"]["run_id"])
    assert selection["pair"]["events"] == expected["items"]
    assert selection["pair"]["events"]
    assert selection["pair_key"] == edge["pair_key"]
    assert {selection["pair"]["account_a"], selection["pair"]["account_b"]} == {
        edge["source"],
        edge["target"],
    }
    enriched = with_selected_pair(detail, selection)
    options = {option["value"] for option in episode_options(enriched, selected)}
    assert options
    assert options <= {event["post_key"] for event in expected["items"]}
