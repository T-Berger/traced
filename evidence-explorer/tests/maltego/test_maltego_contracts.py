"""Maltego joins the shared source contract as a lookup platform, never a polled channel."""

import pytest
from dash import dcc
from dash.development.base_component import Component

from evidence_explorer.collection.models import CollectionFailure
from evidence_explorer.collection.registry import COLLECTABLE_PLATFORMS, normalize_locator
from evidence_explorer.review.integrations import collection_ui
from evidence_explorer.review.source_browser import source_browser_data
from evidence_explorer.source_bundles import build_source_bundle
from evidence_explorer.source_contracts import (
    PLATFORM_LABELS,
    SourceAcquisition,
    SourceCase,
    SourceImport,
    SourceItem,
)


def descendants(node):
    if isinstance(node, Component):
        yield node
        yield from descendants(getattr(node, "children", None))
    elif isinstance(node, list | tuple):
        for child in node:
            yield from descendants(child)


def maltego_item(text="registrar: Example Registrar, LLC"):
    return SourceItem(
        platform="maltego",
        channel_id="datamcp/ci_whois",
        channel_name="[CTI] To WHOIS Records [Fixture]",
        native_id="a" * 64,
        text=text,
        acquired_at="2026-11-07T12:00:00Z",
        provenance=SourceAcquisition(method="maltego_datamcp", content_scope="lookup_result"),
    )


def test_maltego_records_validate_with_their_own_label_and_provenance():
    item = maltego_item()
    assert PLATFORM_LABELS[item.platform] == "Maltego"
    assert item.provenance.method == "maltego_datamcp"


def test_live_collection_neither_offers_nor_accepts_maltego():
    assert "maltego" not in COLLECTABLE_PLATFORMS
    assert set(COLLECTABLE_PLATFORMS) | {"maltego"} == set(PLATFORM_LABELS)
    with pytest.raises(CollectionFailure, match="./traced maltego"):
        normalize_locator("maltego", "example.org")
    dropdown = next(
        node
        for node in descendants(collection_ui.source_picker())
        if isinstance(node, dcc.Dropdown) and node.id == "live-platform"
    )
    assert [option["value"] for option in dropdown.options] == list(COLLECTABLE_PLATFORMS)


def test_source_browser_offers_a_maltego_filter_for_saved_lookup_records():
    case = SourceCase(title="Example", question="Who registered example.org?")
    bundle = build_source_bundle(
        SourceImport(source_schema="1.0", case=case, items=[maltego_item()])
    )
    data = source_browser_data(bundle)
    assert {"label": "Maltego", "value": "maltego"} in data["platforms"]
