"""Aleph checks verify access without leaking credentials or following redirects."""

import httpx
import pytest

from evidence_explorer.collection.aleph import aleph_configuration, check_aleph_connection

ORIGIN = "https://aleph.example.org"
KEY = "7 fixture-only-secret"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    monkeypatch.delenv("TRACED_ALEPH_BASE_URL", raising=False)
    monkeypatch.delenv("TRACED_ALEPH_API_KEY", raising=False)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("TRACED_ALEPH_BASE_URL", ORIGIN + "/")
    monkeypatch.setenv("TRACED_ALEPH_API_KEY", KEY)


def public_dns(host):
    assert host == "aleph.example.org"
    return ["93.184.215.14"]


def unexpected_request(_request):
    pytest.fail("The check must not send a request in this state.")


def test_key_never_falls_back_to_default_instance(monkeypatch):
    monkeypatch.setenv("TRACED_ALEPH_API_KEY", KEY)
    config = aleph_configuration()
    assert config.instance_url == "https://aleph.occrp.org"
    assert not config.ready
    result = check_aleph_connection(transport=httpx.MockTransport(unexpected_request))
    assert result.status == "setup_required"
    assert KEY not in repr(config) + repr(result)


@pytest.mark.parametrize(
    "origin",
    [
        "http://aleph.example.org",
        "https://aleph.example.org/api/2",
        "https://aleph.example.org/?token=fixture-only-secret",
        "https://fixture-only-secret@aleph.example.org",
        "https://127.0.0.1",
    ],
)
def test_malformed_or_private_origin_is_not_used(monkeypatch, origin):
    monkeypatch.setenv("TRACED_ALEPH_BASE_URL", origin)
    monkeypatch.setenv("TRACED_ALEPH_API_KEY", KEY)
    config = aleph_configuration()
    result = check_aleph_connection(transport=httpx.MockTransport(unexpected_request))
    assert config.instance_url is None and not config.ready
    assert result.status == "setup_required"
    assert "fixture-only-secret" not in repr(config) + repr(result)


@pytest.mark.parametrize("key", ["", "bad\r\nheader", "non-ascii-é"])
def test_missing_or_invalid_key_never_reaches_network(monkeypatch, key):
    monkeypatch.setenv("TRACED_ALEPH_BASE_URL", ORIGIN)
    monkeypatch.setenv("TRACED_ALEPH_API_KEY", key)
    assert not aleph_configuration().ready
    result = check_aleph_connection(transport=httpx.MockTransport(unexpected_request))
    assert result.status == "setup_required"


def test_collection_check_uses_documented_bounded_request(configured):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url == ORIGIN + "/api/2/collections?limit=1"
        assert request.headers["Authorization"] == "ApiKey " + KEY
        assert request.headers["Accept"] == "application/json"
        return httpx.Response(200, json={"results": [{"label": "Private collection name"}]})

    config = aleph_configuration()
    result = check_aleph_connection(transport=httpx.MockTransport(respond), resolver=public_dns)
    assert config.ready and config.instance_url == ORIGIN
    assert result.status == "connected" and result.http_status == 200
    assert len(calls) == 1
    assert KEY not in repr(config) + repr(result)
    assert "Private collection name" not in repr(result)


@pytest.mark.parametrize(
    ("http_status", "expected_status", "message"),
    [
        (401, "setup_required", "rejected the API key"),
        (403, "provider_unavailable", "denied access"),
        (429, "provider_unavailable", "rate limiting"),
    ],
)
def test_access_failure_is_specific_and_redacted(configured, http_status, expected_status, message):
    transport = httpx.MockTransport(lambda _: httpx.Response(http_status, text=KEY))
    result = check_aleph_connection(transport=transport, resolver=public_dns)
    assert result.status == expected_status and result.http_status == http_status
    assert message in result.message
    assert KEY not in repr(result)


def test_redirect_never_forwards_key(configured):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://another.example.org/"})

    result = check_aleph_connection(transport=httpx.MockTransport(respond), resolver=public_dns)
    assert result.status == "provider_unavailable"
    assert "redirected" in result.message
    assert len(calls) == 1 and calls[0].url.host == "aleph.example.org"
    assert "another.example.org" not in repr(result)


def test_private_dns_result_is_rejected_before_request(configured):
    result = check_aleph_connection(
        transport=httpx.MockTransport(unexpected_request),
        resolver=lambda _: ["93.184.215.14", "10.0.0.5"],
    )
    assert result.status == "provider_unavailable"
    assert "public network address" in result.message


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text=KEY),
        httpx.Response(200, json={"message": KEY}),
        httpx.Response(200, json={"results": [KEY * 8192]}),
    ],
)
def test_invalid_or_oversized_response_is_not_a_connection(configured, response):
    result = check_aleph_connection(
        transport=httpx.MockTransport(lambda _: response), resolver=public_dns
    )
    assert result.status == "provider_unavailable"
    assert KEY not in repr(result)
