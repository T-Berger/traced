"""Explicit specialist history is reachable without becoming support or a decision."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from dash import dcc, html
from dash.development.base_component import Component

from evidence_explorer.review.integrations.specialist_context import (
    MAX_CONTEXT_BYTES,
    READ_ONLY_NOTICE,
    specialist_history_context,
)
from evidence_explorer.review.selectors import evidence_ids


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


def context_bundle(snapshot):
    saved = json.dumps(snapshot, ensure_ascii=False)
    finding = {
        "snapshot_id": "finding:current",
        "details": {
            "specialist_history_context": {
                "label": "Open saved specialist history · read only",
                "purpose": "Inspect past revisions and the preserved exclusion experiment.",
                "limitation": "Specialist Reviewed is not shared approval.",
                "record_ref": {"type": "record", "id": "record:context"},
                "fragment_ref": {"type": "fragment", "id": "fragment:context"},
                "read_only": True,
            }
        },
    }
    bundle = {
        "records": [
            {"id": "record:frame", "kind": "image"},
            {
                "id": "record:context",
                "kind": "specialist_brief_snapshot",
                "source_label": "Immutable specialist snapshot",
                "text": saved,
            },
        ],
        "fragments": [
            {
                "id": "fragment:context",
                "record_id": "record:context",
                "selector": {"kind": "text", "start": 0, "end": len(saved)},
                "quote": saved,
            }
        ],
        "findings": [finding],
        "links": [
            {
                "source": {"type": "record", "id": "record:frame"},
                "target": {"type": "finding", "id": "finding:current"},
                "relation": "supports",
                "fragment_ids": [],
            }
        ],
        "reviews": [],
    }
    return bundle, finding


def test_disclosure_exposes_saved_text_without_global_selection_or_mutating_controls():
    bundle, finding = context_bundle(
        {
            "review_history": {
                "items": [
                    {"reviewer": "Alice", "status": "draft"},
                    {"reviewer": "Bob", "status": "reviewed"},
                ],
                "total": 2,
                "truncated": False,
            },
            "lineage": {"items": [{"scenario_id": "fork"}], "total": 1, "truncated": False},
            "exclusion_events": {
                "items": [{"excluded": True}, {"excluded": False}],
                "total": 2,
                "truncated": False,
            },
        }
    )
    original = deepcopy(bundle)
    support_before = evidence_ids(bundle, "finding:current")
    rendered = specialist_history_context(bundle, finding)
    visible = text(rendered)
    assert READ_ONLY_NOTICE in visible
    assert "2 retained of 2" in visible and "Scenario lineage: 1 retained of 1" in visible
    assert '"reviewer": "Alice"' in visible and '"reviewer": "Bob"' in visible
    disclosures = [node for node in components(rendered) if isinstance(node, html.Details)]
    assert disclosures and disclosures[0].open is False
    assert not any(
        isinstance(
            node,
            (html.Button, dcc.Input, dcc.Textarea, dcc.Store, dcc.Dropdown, dcc.RadioItems),
        )
        for node in components(rendered)
    )
    assert evidence_ids(bundle, "finding:current") == support_before == ({"record:frame"}, set())
    assert bundle == original
    assert bundle["reviews"] == []


def test_legacy_absent_sections_stay_unknown_instead_of_zero():
    bundle, finding = context_bundle({"title": "Legacy snapshot"})
    rendered = text(specialist_history_context(bundle, finding))
    assert rendered.count("not recorded in this snapshot") == 3
    assert "0 retained" not in rendered


def test_declared_truncation_and_unknown_earlier_origin_remain_visible():
    bundle, finding = context_bundle(
        {
            "review_history": {"items": [{"revision": 8}], "total": 80, "truncated": True},
            "lineage": {
                "items": [{"scenario_id": "fork"}],
                "total": 1,
                "total_is_exact": False,
                "truncated": False,
                "origin_unavailable": True,
            },
        }
    )
    rendered = text(specialist_history_context(bundle, finding))
    assert "1 retained of 80" in rendered and "Truncated" in rendered
    assert "at least 1" in rendered and "Earlier origin is outside this package" in rendered


@pytest.mark.parametrize("change", ["wrong_type", "missing_record", "wrong_fragment", "bad_quote"])
def test_unresolvable_typed_reference_never_falls_back_to_current_support(change):
    bundle, finding = context_bundle(
        {"private_context": "Should not render through an invalid pointer"}
    )
    context = finding["details"]["specialist_history_context"]
    if change == "wrong_type":
        context["record_ref"]["type"] = "finding"
    elif change == "missing_record":
        context["record_ref"]["id"] = "record:absent"
    elif change == "wrong_fragment":
        bundle["fragments"][0]["record_id"] = "record:frame"
    else:
        bundle["fragments"][0]["quote"] = "changed"
    rendered = text(specialist_history_context(bundle, finding))
    assert "unavailable" in rendered
    assert "Should not render" not in rendered
    assert evidence_ids(bundle, "finding:current")[0] == {"record:frame"}


def test_context_is_opt_in_and_oversized_record_fails_closed():
    bundle, finding = context_bundle({})
    assert specialist_history_context(bundle, {"details": {}}) == ""
    bundle["records"][1]["text"] = "x" * (MAX_CONTEXT_BYTES + 1)
    rendered = text(specialist_history_context(bundle, finding))
    assert "2 MiB display limit" in rendered
    assert len(rendered) < 1000
