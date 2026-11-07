"""Official API adapters keep provenance, access errors, and collection bounds explicit."""

import base64
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from evidence_explorer.collection.apis import (
    MAX_API_BYTES,
    _APIClient,
    collect_api,
    configuration_status,
)
from evidence_explorer.collection.models import CollectionFailure, CollectionSource


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    for name in (
        "X_BEARER_TOKEN",
        "REDDIT_ACCESS_TOKEN",
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_USER_AGENT",
    ):
        monkeypatch.delenv(name, raising=False)


def source(platform="x", **changes):
    return CollectionSource(
        id="test-source",
        platform=platform,
        label=f"Test {platform} source",
        locator=changes.pop("locator", "sample" if platform == "x" else "worldnews"),
        **changes,
    )


def x_post(native="100", **changes):
    return {
        "id": native,
        "author_id": "123",
        "text": "The bridge inspection starts tomorrow.",
        "created_at": "2026-11-06T12:00:00Z",
        **changes,
    }


def x_transport(posts, requests, *, meta=None, includes=None):
    def respond(request):
        requests.append(request)
        assert request.url.host == "api.x.com"
        assert request.headers["Authorization"] == "Bearer fixture-x-token"
        if request.url.path.startswith("/2/users/by/username/"):
            return httpx.Response(200, json={"data": {"id": "123", "username": "sample"}})
        assert request.url.path == "/2/users/123/tweets"
        return httpx.Response(
            200, json={"data": posts, "meta": meta or {}, "includes": includes or {}}
        )

    return httpx.MockTransport(respond)


def reddit_post(native="abc", **changes):
    return {
        "kind": "t3",
        "data": {
            "id": native,
            "name": "t3_" + native,
            "subreddit": "worldnews",
            "title": "Bridge inspection",
            "selftext": "An inspection was announced.",
            "permalink": f"/r/worldnews/comments/{native}/bridge_inspection/",
            "author": "public-author",
            "created_utc": 1793966400,
            **changes,
        },
    }


def reddit_transport(posts, requests, *, after=None):
    def respond(request):
        requests.append(request)
        assert request.url.host == "oauth.reddit.com"
        assert request.url.path == "/r/worldnews/new"
        assert request.headers["Authorization"] == "Bearer fixture-reddit-token"
        return httpx.Response(
            200, json={"kind": "Listing", "data": {"children": posts, "after": after}}
        )

    return httpx.MockTransport(respond)


@pytest.mark.parametrize("platform", ["x", "reddit"])
def test_missing_credentials_fail_before_network(platform):
    status = configuration_status(platform)
    assert status["ready"] is False
    calls = []
    with pytest.raises(CollectionFailure, match="Connect"):
        collect_api(
            source(platform), transport=httpx.MockTransport(lambda request: calls.append(request))
        )
    assert calls == []


def test_status_only_reports_configuration_presence(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "do-not-display-this-token")
    monkeypatch.setenv("REDDIT_CLIENT_ID", "do-not-display-id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "do-not-display-secret")
    for platform in ("x", "reddit"):
        state = configuration_status(platform)
        assert state["ready"] is True
        assert "do-not-display" not in json.dumps(state)
        assert "access" in state["message"].lower()
    assert configuration_status("news")["ready"] is False


def test_x_maps_explicit_post_fields_and_incremental_cursor(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "fixture-x-token")
    calls = []
    batch = collect_api(
        source(locator="https://x.com/Sample", cursor="80"),
        transport=x_transport(
            [
                x_post(
                    "100",
                    entities={
                        "urls": [
                            {"expanded_url": "https://publisher.example/story"},
                            {"expanded_url": "javascript:alert(1)"},
                        ]
                    },
                ),
                x_post("99", created_at=None),
            ],
            calls,
        ),
    )
    assert len(calls) == 2
    assert calls[1].url.params["since_id"] == "80"
    assert calls[1].url.params["max_results"] == "5"
    assert calls[0].url.path == "/2/users/by/username/sample"
    first, unknown = batch.items
    assert first.channel_id == "123"
    assert first.native_id == "100"
    assert first.author_id == "123"
    assert first.text == "The bridge inspection starts tomorrow."
    assert first.published_at == "2026-11-06T12:00:00Z"
    assert unknown.published_at is None
    assert first.shared_urls == ["https://publisher.example/story"]
    assert first.source_url == "https://x.com/sample/status/100"
    assert first.provenance.method == "x_api"
    assert first.provenance.retrieved_url == "https://api.x.com/2/users/123/tweets"
    assert "fixture-x-token" not in first.model_dump_json()
    assert datetime.fromisoformat(first.acquired_at).tzinfo == UTC
    assert batch.cursor == "100"


def test_x_explicit_retweet_has_namespaced_original_only_with_author_context(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "fixture-x-token")
    posts = [
        x_post("100", referenced_tweets=[{"type": "retweeted", "id": "70"}]),
        x_post("99", referenced_tweets=[{"type": "quoted", "id": "70"}]),
        x_post("98", referenced_tweets=[{"type": "retweeted", "id": "60"}]),
    ]
    batch = collect_api(
        source(),
        transport=x_transport(posts, [], includes={"tweets": [{"id": "70", "author_id": "200"}]}),
    )
    assert batch.items[0].repost_of.model_dump() == {
        "platform": "x",
        "channel_id": "200",
        "native_id": "70",
    }
    assert batch.items[1].repost_of is None
    assert batch.items[2].repost_of is None


def test_x_limit_and_partial_fields_never_silently_advance_cursor(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "fixture-x-token")
    batch = collect_api(
        source(item_limit=1, cursor="50"),
        transport=x_transport([x_post("100"), x_post("99")], [], meta={"next_token": "next"}),
    )
    assert len(batch.items) == 1
    assert batch.cursor == "50"
    assert any("has not advanced" in warning for warning in batch.warnings)
    partial = collect_api(
        source(cursor="50"), transport=x_transport([x_post("100"), {"id": "99"}], [])
    )
    assert len(partial.items) == 1
    assert partial.cursor == "50"
    assert "omitted 1" in partial.warnings[0]


def test_x_empty_window_retains_cursor_and_never_invents_records(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "fixture-x-token")
    batch = collect_api(source(cursor="100"), transport=x_transport([], []))
    assert batch.items == ()
    assert batch.cursor == "100"


@pytest.mark.parametrize(
    "locator", ["https://evil.example/sample", "sample/../../tokens", "@bad name"]
)
def test_x_invalid_locator_never_sends_a_token(monkeypatch, locator):
    monkeypatch.setenv("X_BEARER_TOKEN", "fixture-x-token")
    calls = []
    with pytest.raises(CollectionFailure):
        collect_api(source(locator=locator), transport=x_transport([], calls))
    assert calls == []


@pytest.mark.parametrize("code", [301, 302, 401, 402, 403, 404, 500])
def test_api_errors_do_not_echo_credentials_or_follow_redirects(monkeypatch, code):
    monkeypatch.setenv("X_BEARER_TOKEN", "fixture-x-token")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            code,
            text="secret fixture-x-token malicious response body",
            headers={"location": "https://attacker.example/steal"},
        )

    with pytest.raises(CollectionFailure) as caught:
        collect_api(source(), transport=httpx.MockTransport(respond))
    assert "fixture-x-token" not in str(caught.value)
    assert "malicious response" not in str(caught.value)
    assert len(calls) == 1
    assert calls[0].url.host == "api.x.com"


def test_api_rate_limit_honors_bounded_retry_after(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "fixture-x-token")
    with pytest.raises(CollectionFailure) as caught:
        collect_api(
            source(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(429, headers={"Retry-After": "123"})
            ),
        )
    assert caught.value.retry_after_seconds == 123
    assert "rate limit" in str(caught.value)


def test_transport_exception_is_sanitized(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "fixture-x-token")

    def fail(request):
        raise httpx.ConnectError("fixture-x-token proxy password", request=request)

    with pytest.raises(CollectionFailure) as caught:
        collect_api(source(), transport=httpx.MockTransport(fail))
    assert "fixture-x-token" not in str(caught.value)
    assert "proxy password" not in str(caught.value)


@pytest.mark.parametrize("body", [b"[1,2]", b"not JSON", b'{"data":1}'])
def test_malformed_api_responses_fail_without_accepting_records(monkeypatch, body):
    monkeypatch.setenv("X_BEARER_TOKEN", "fixture-x-token")
    with pytest.raises(CollectionFailure):
        collect_api(
            source(),
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
        )


def test_response_size_limit_applies_to_declared_and_actual_downloads(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "fixture-x-token")
    for response in (
        httpx.Response(200, headers={"content-length": str(MAX_API_BYTES + 1)}),
        httpx.Response(200, content=b"x" * (MAX_API_BYTES + 1)),
    ):
        with pytest.raises(CollectionFailure, match="download limit"):
            collect_api(
                source(), transport=httpx.MockTransport(lambda request, result=response: result)
            )


def test_reddit_maps_post_body_public_permalink_and_explicit_crosspost(monkeypatch):
    monkeypatch.setenv("REDDIT_ACCESS_TOKEN", "fixture-reddit-token")
    monkeypatch.setenv("REDDIT_USER_AGENT", "python:local.evidence-tests:v1 (by /u/researcher)")
    calls = []
    batch = collect_api(
        source("reddit"),
        transport=reddit_transport(
            [
                reddit_post(
                    crosspost_parent_list=[{"id": "original", "subreddit": "Europe"}],
                    url_overridden_by_dest="https://publisher.example/story",
                ),
                reddit_post("def", created_utc=None, author="[deleted]"),
            ],
            calls,
        ),
    )
    assert len(calls) == 1
    assert calls[0].url.params["limit"] == "5"
    assert calls[0].url.params["raw_json"] == "1"
    assert calls[0].headers["User-Agent"].endswith("(by /u/researcher)")
    first, unknown = batch.items
    assert first.platform == "reddit"
    assert first.channel_id == "worldnews"
    assert first.native_id == "abc"
    assert first.title == "Bridge inspection"
    assert first.text == "Bridge inspection\n\nAn inspection was announced."
    assert first.shared_urls == ["https://publisher.example/story"]
    assert first.source_url == "https://www.reddit.com/r/worldnews/comments/abc/bridge_inspection/"
    assert first.repost_of.model_dump() == {
        "platform": "reddit",
        "channel_id": "europe",
        "native_id": "original",
    }
    assert first.provenance.method == "reddit_api"
    assert first.provenance.retrieved_url == "https://oauth.reddit.com/r/worldnews/new"
    assert unknown.author_id is None
    assert unknown.published_at is None
    assert batch.cursor == "t3_abc"


def test_reddit_app_credentials_exchange_and_listing_use_separate_fixed_hosts(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "fixture-app")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "fixture-secret")
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.host == "www.reddit.com":
            assert request.method == "POST"
            assert request.url.path == "/api/v1/access_token"
            assert parse_qs(request.content.decode()) == {"grant_type": ["client_credentials"]}
            expected = base64.b64encode(b"fixture-app:fixture-secret").decode()
            assert request.headers["Authorization"] == "Basic " + expected
            return httpx.Response(
                200, json={"access_token": "ephemeral-token", "token_type": "bearer"}
            )
        assert request.url.host == "oauth.reddit.com"
        assert request.headers["Authorization"] == "Bearer ephemeral-token"
        return httpx.Response(200, json={"kind": "Listing", "data": {"children": [reddit_post()]}})

    batch = collect_api(source("reddit"), transport=httpx.MockTransport(respond))
    assert len(calls) == 2
    assert len(batch.items) == 1
    for secret in ("fixture-secret", "ephemeral-token", "fixture-app"):
        assert secret not in batch.items[0].model_dump_json()


def test_reddit_access_token_takes_precedence_over_app_credentials(monkeypatch):
    monkeypatch.setenv("REDDIT_ACCESS_TOKEN", "fixture-reddit-token")
    monkeypatch.setenv("REDDIT_CLIENT_ID", "unused-id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "unused-secret")
    calls = []
    collect_api(source("reddit"), transport=reddit_transport([], calls))
    assert len(calls) == 1


def test_reddit_omits_deleted_invalid_or_wrong_community_items(monkeypatch):
    monkeypatch.setenv("REDDIT_ACCESS_TOKEN", "fixture-reddit-token")
    batch = collect_api(
        source("reddit", item_limit=5),
        transport=reddit_transport(
            [
                reddit_post("aaa", selftext="[removed]"),
                reddit_post("bbb", subreddit="unexpected"),
                reddit_post("ccc", permalink="https://attacker.example/credentials"),
                reddit_post("ddd", created_utc=True),
                reddit_post("eee"),
                reddit_post("fff"),
            ],
            [],
            after="t3_fff",
        ),
    )
    assert [item.native_id for item in batch.items] == ["eee"]
    assert any("omitted 4" in warning for warning in batch.warnings)
    assert any("window" in warning for warning in batch.warnings)


def test_private_resolver_result_blocks_credentials_before_request(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "fixture-x-token")
    with _APIClient("x", resolver=lambda hostname: ["127.0.0.1"]) as client:
        with pytest.raises(CollectionFailure, match="safely"):
            client.request(
                "GET",
                "https://api.x.com/2/users/123/tweets",
                headers={"Authorization": "Bearer fixture-x-token"},
            )


def test_unexpected_api_destination_is_rejected_before_credentials_are_sent():
    calls = []
    with _APIClient(
        "x", transport=httpx.MockTransport(lambda request: calls.append(request))
    ) as client:
        with pytest.raises(CollectionFailure, match="unexpected"):
            client.request(
                "GET", "https://attacker.example/api", headers={"Authorization": "secret"}
            )
    assert calls == []
