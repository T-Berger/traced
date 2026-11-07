"""Canonical saved-source boundaries preserve identity and distinct evidence kinds."""

from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy

import pytest

from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.importing import MAX_IMPORT_BYTES, ImportValidationError
from evidence_explorer.review.integrations.sources import (
    MAX_SOURCE_ITEMS,
    SourceImport,
    build_source_bundle,
    parse_source_import,
    source_browser_data,
)


def _item(**changes):
    return {
        "platform": "telegram",
        "channel_id": "channel-a",
        "channel_name": "Channel A",
        "native_id": "item-1",
        "text": "Оригінальний текст.\nOriginal saved text.",
        "acquired_at": "2026-11-06T10:00:00+01:00",
        **changes,
    }


def _payload(*items):
    return {
        "source_schema": "1.0",
        "case": {"title": "A supplied collection", "question": "What is observed?"},
        "items": list(items) or [_item()],
    }


def test_saved_text_hash_unknown_publication_and_collection_observation():
    bundle = build_source_bundle(_payload())
    assert bundle.case.synthetic is False
    assert bundle.case.id.startswith("sources:")
    assert bundle.source_import == {"source_schema": "1.0"}
    record = bundle.records[0]
    assert record.text == _item()["text"]
    assert record.content_hash == hashlib.sha256(record.text.encode()).hexdigest()
    assert record.published_at is None
    assert record.acquired_at == "2026-11-06T09:00:00Z"
    assert bundle.fragments[0].quote == record.text
    assert bundle.case.coverage["unknown_publication_times"] == 1
    finding = bundle.findings[0]
    assert finding.kind == "observation"
    support = next(link for link in bundle.links if link.relation == "supports")
    assert support.source.id == record.id
    assert support.target.id == finding.snapshot_id
    assert (
        EvidenceBundle.model_validate_json(bundle.model_dump_json()).fingerprint()
        == bundle.fingerprint()
    )
    browser = source_browser_data(bundle)
    assert browser["records"][0]["published_label"] == "Unknown publication time"


def test_unchanged_reimport_and_input_reordering_are_idempotent_but_edits_are_new_snapshots():
    first, second = _item(), _item(platform="reddit", native_id="second")
    payload = _payload(first, second)
    bundle = build_source_bundle(payload)
    same = build_source_bundle(SourceImport.model_validate(_payload(second, first)))
    assert same.model_dump(mode="json") == bundle.model_dump(mode="json")
    assert parse_source_import(json.dumps(payload)).fingerprint() == bundle.fingerprint()
    changed = deepcopy(payload)
    changed["items"][0]["text"] += " Added context."
    updated = build_source_bundle(changed)
    assert updated.case.id != bundle.case.id
    assert updated.runs[0].id != bundle.runs[0].id
    assert updated.findings[0].snapshot_id != bundle.findings[0].snapshot_id
    before = {record.metadata["platform"]: record.id for record in bundle.records}
    after = {record.metadata["platform"]: record.id for record in updated.records}
    assert before["telegram"] != after["telegram"]
    assert before["reddit"] == after["reddit"]


def test_platform_and_channel_namespace_prevent_record_and_author_collisions():
    bundle = build_source_bundle(
        _payload(
            _item(author_id="shared-handle"),
            _item(platform="x", author_id="shared-handle"),
            _item(channel_id="channel-b", channel_name="Channel B", author_id="shared-handle"),
            _item(native_id="another-item", author_id="shared-handle"),
        )
    )
    assert len({record.id for record in bundle.records}) == 4
    assert len({record.metadata["channel_key"] for record in bundle.records}) == 3
    assert len({record.metadata["author_key"] for record in bundle.records}) == 3
    assert not [link for link in bundle.links if link.relation == "documented_repost"]


def test_delimiters_in_native_identifiers_cannot_collide():
    bundle = build_source_bundle(
        _payload(
            _item(channel_id="a:b", native_id="c"),
            _item(channel_id="a", native_id="b:c"),
        )
    )
    assert len({record.id for record in bundle.records}) == 2


def test_exact_text_shared_urls_and_documented_reposts_are_separate_evidence():
    url = "https://example.org/story?id=1#section"
    original = _item(shared_urls=[url])
    exact = _item(platform="x", shared_urls=[url, url])
    same_url = _item(platform="reddit", text="Different saved wording", shared_urls=[url])
    repost = _item(
        platform="youtube",
        text="An explicitly referenced copy with context",
        repost_of={"platform": "telegram", "channel_id": "channel-a", "native_id": "item-1"},
    )
    bundle = build_source_bundle(_payload(original, exact, same_url, repost))
    ids = {record.metadata["platform"]: record.id for record in bundle.records}
    content_links = [link for link in bundle.links if link.relation == "same_content"]
    assert len(content_links) == 1
    assert {content_links[0].source.id, content_links[0].target.id} == {ids["telegram"], ids["x"]}
    repost_links = [link for link in bundle.links if link.relation == "documented_repost"]
    assert len(repost_links) == 1
    assert repost_links[0].source.id == ids["telegram"]
    assert repost_links[0].target.id == ids["youtube"]
    browser = source_browser_data(bundle)
    assert browser["exact_groups"][0]["count"] == 2
    assert browser["url_groups"][0]["count"] == 3
    assert len(browser["reposts"]) == 1
    assert "actor identity" in browser["url_groups"][0]["basis"]
    assert not [finding for finding in bundle.findings if finding.kind == "inference"]


def test_unresolved_repost_is_preserved_without_inventing_a_link():
    bundle = build_source_bundle(
        _payload(
            _item(repost_of={"platform": "news", "channel_id": "outside", "native_id": "missing"})
        )
    )
    assert bundle.records[0].metadata["repost_of"]["native_id"] == "missing"
    assert bundle.case.coverage["unresolved_repost_references"] == 1
    assert not [link for link in bundle.links if link.relation == "documented_repost"]


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,unsafe",
        "https://user:pass@example.org/post",
        "https:///no-host",
        "https://unknown/post",
        "//example.org/post",
        "https://127.0.0.1/post",
        "https://example.org/white space",
        "https://example.org/\nsecret",
        "https://[::1]/post",
        "https://example.org:99999/post",
        "https://bad_host.example/post",
    ],
)
def test_unsafe_urls_fail_without_echoing_source_input(url):
    with pytest.raises(ImportValidationError) as error:
        build_source_bundle(_payload(_item(source_url=url, text="PRIVATE_SOURCE_TEXT")))
    assert url not in str(error.value)
    assert "PRIVATE_SOURCE_TEXT" not in str(error.value)


@pytest.mark.parametrize("field", ["source_url", "shared_urls"])
def test_source_and_shared_urls_use_the_same_policy(field):
    value = "https://name:secret@example.org/post"
    with pytest.raises(ImportValidationError):
        build_source_bundle(
            _payload(_item(**{field: [value] if field == "shared_urls" else value}))
        )


@pytest.mark.parametrize(
    "change",
    [
        {"platform": "unknown"},
        {"native_id": 42},
        {"text": ""},
        {"text": "x" * 50_001},
        {"published_at": "2026-11-06T10:00:00"},
        {"acquired_at": None},
        {"acquired_at": "2026-11-06"},
        {"author_id": []},
        {"channel_id": " "},
        {"shared_urls": "https://example.org/a"},
        {"extra_field": "PRIVATE_SOURCE_TEXT"},
    ],
)
def test_strict_item_boundaries_reject_invalid_values(change):
    with pytest.raises(ImportValidationError) as error:
        build_source_bundle(_payload(_item(**change)))
    assert "PRIVATE_SOURCE_TEXT" not in str(error.value)


def test_duplicate_identities_conflicting_channels_and_self_reposts_are_rejected():
    for payload in [
        _payload(_item(), _item(text="Changed duplicate")),
        _payload(_item(), _item(native_id="second", channel_name="Conflicting name")),
        _payload(
            _item(
                repost_of={"platform": "telegram", "channel_id": "channel-a", "native_id": "item-1"}
            )
        ),
    ]:
        with pytest.raises(ImportValidationError):
            build_source_bundle(payload)


def test_item_count_and_finite_json_strictness_are_bounded():
    with pytest.raises(ImportValidationError):
        build_source_bundle({**_payload(), "items": []})
    with pytest.raises(ImportValidationError):
        build_source_bundle(_payload(*[_item()] * (MAX_SOURCE_ITEMS + 1)))
    for text in [
        '{"source_schema":"1.0","source_schema":"1.0"}',
        '{"source_schema":"1.0","case":{"synthetic":NaN}}',
        "[1,2,3]",
        '{"source_schema":' + "1" * 5000 + "}",
        '{"sensitive_field_with_underscore":"PRIVATE_SOURCE_TEXT"}',
    ]:
        with pytest.raises(ImportValidationError) as error:
            parse_source_import(text)
        assert "PRIVATE_SOURCE_TEXT" not in str(error.value)
        assert "sensitive_field_with_underscore" not in str(error.value)


def test_parser_accepts_uploads_and_reports_safe_encoding_size_and_syntax_errors():
    raw = json.dumps(_payload()).encode()
    uri = "data:application/json;base64," + base64.b64encode(raw).decode()
    assert parse_source_import(uri, "sources.json").case.id == parse_source_import(raw).case.id
    for content, filename in [
        (raw, "sources.csv"),
        (b"\xff", "sources.json"),
        ("data:application/json;base64,%%%", None),
        ("{" + " " * MAX_IMPORT_BYTES, None),
        ('{"items": invalid PRIVATE_SOURCE_TEXT}', None),
    ]:
        with pytest.raises(ImportValidationError) as error:
            parse_source_import(content, filename)
        assert "PRIVATE_SOURCE_TEXT" not in str(error.value)


def test_browser_orders_actual_utc_times_with_unknown_last_and_bounds_groups():
    bundle = build_source_bundle(
        _payload(
            _item(
                native_id="later",
                published_at="2026-11-06T10:00:00.5+01:00",
                acquired_at="2026-11-06T09:00:00.5Z",
            ),
            _item(
                native_id="earlier",
                published_at="2026-11-06T09:00:00Z",
                acquired_at="2026-11-06T09:00:00Z",
            ),
            _item(native_id="unknown"),
        )
    )
    assert bundle.runs[0].created_at == "2026-11-06T09:00:00.500000Z"
    browser = source_browser_data(bundle, limit=2)
    assert [record["native_id"] for record in browser["records"]] == ["earlier", "later"]
    assert browser["total"] == browser["filtered_count"] == 3
    assert browser["truncated"] is True
    assert browser["exact_groups"][0]["count"] == 3
    assert browser["exact_groups"][0]["truncated"] is True
    assert len(browser["exact_groups"][0]["record_ids"]) == 2
    assert source_browser_data(bundle)["records"][-1]["native_id"] == "unknown"


def test_browser_filters_by_platform_and_namespaced_channel():
    bundle = build_source_bundle(_payload(_item(), _item(platform="x")))
    browser = source_browser_data(bundle)
    assert len(browser["platforms"]) == len(browser["channels"]) == 2
    telegram = next(record for record in browser["records"] if record["platform"] == "telegram")
    selected = source_browser_data(bundle, channels=[telegram["channel_key"]])
    assert selected["filtered_count"] == 1
    assert selected["records"][0]["platform"] == "telegram"
    assert source_browser_data(bundle, platforms=["x"])["filtered_count"] == 1
    assert source_browser_data(bundle, platforms=["absent"])["filtered_count"] == 0
    assert source_browser_data(bundle, platforms=[])["filtered_count"] == 2
    for limit in [0, -1, True, 201, "2"]:
        with pytest.raises(ValueError):
            source_browser_data(bundle, limit=limit)


def test_legacy_news_publishers_and_telegram_channel_metadata_group_consistently():
    def record(identifier, kind, label, metadata):
        return {
            "id": identifier,
            "kind": kind,
            "source_label": label,
            "text": identifier,
            "metadata": metadata,
        }

    bundle = {
        "records": [
            record("news-a", "article", "Publisher · Title A", {"publisher": "Publisher"}),
            record("news-b", "article", "Publisher · Title B", {"publisher": "Publisher"}),
            record(
                "channel",
                "telegram_channel",
                "Supplied channel",
                {"swarm": {"channel_key": "c", "name": "A named channel"}},
            ),
            record("post", "telegram_post", "Supplied parent", {"swarm": {"channel_key": "c"}}),
            record(
                "message",
                "telegram_message",
                "Supplied message",
                {"swarm": {"channel_key": "c", "account_key": "not-a-global-actor"}},
            ),
        ]
    }
    browser = source_browser_data(bundle)
    assert len(browser["channels"]) == 2
    assert {option["label"] for option in browser["channels"]} == {
        "News · Publisher",
        "Telegram · A named channel",
    }
    assert browser["total"] == 5
    assert all(record["author_key"] is None for record in browser["records"])
    assert all(
        record["published_label"] == "Unknown publication time" for record in browser["records"]
    )


def test_exact_text_preserves_whitespace_instead_of_fuzzy_matching():
    bundle = build_source_bundle(_payload(_item(text="same"), _item(platform="x", text="same ")))
    assert not source_browser_data(bundle)["exact_groups"]
    assert not [link for link in bundle.links if link.relation == "same_content"]
