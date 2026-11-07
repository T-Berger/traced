"""Wayback only queries the public availability service for explicit public URLs."""

from datetime import date, datetime

import httpx
import pytest

from evidence_explorer.collection.models import CollectionFailure
from evidence_explorer.collection.wayback import lookup_wayback

PAGE = "https://www.nasa.gov/"
ARCHIVE = "http://web.archive.org/web/20250101231552/https://www.nasa.gov/"
CAPTURE = {"available": True, "url": ARCHIVE, "timestamp": "20250101231552", "status": "200"}


def lookup(response, url=PAGE, capture_date=None, *, resolver=None):
    requests = []

    def respond(request):
        requests.append(request)
        return response

    result = lookup_wayback(
        url,
        capture_date,
        transport=httpx.MockTransport(respond),
        resolver=resolver or (lambda _hostname: ["93.184.216.34"]),
    )
    return result, requests


def test_lookup_only_requests_availability_and_preserves_capture_time():
    result, requests = lookup(
        httpx.Response(200, json={"archived_snapshots": {"closest": CAPTURE}}),
        capture_date=date(2025, 1, 1),
    )
    assert len(requests) == 1
    request = requests[0]
    assert (request.url.scheme, request.url.host, request.url.path) == (
        "https",
        "archive.org",
        "/wayback/available",
    )
    assert dict(request.url.params) == {"url": PAGE, "timestamp": "20250101"}
    assert result.found and result.original_url == PAGE
    assert result.archive_url == ARCHIVE.replace("http:", "https:", 1)
    assert result.captured_at == "2025-01-01T23:15:52+00:00"
    assert datetime.fromisoformat(result.checked_at).tzinfo is not None


def test_empty_lookup_reports_no_capture_and_requests_latest():
    result, requests = lookup(httpx.Response(200, json={"archived_snapshots": {}}))
    assert dict(requests[0].url.params) == {"url": PAGE}
    assert not result.found and result.archive_url is None and result.captured_at is None


@pytest.mark.parametrize(
    "url",
    [
        "https://www.nasa.gov/?token=secret",
        "https://www.nasa.gov/#private",
        "https://user:secret@www.nasa.gov/",
        "https://127.0.0.1/",
        "https://www.nasa.gov:444/",
    ],
)
def test_unsuitable_input_is_rejected_before_sending_to_archive(url):
    def never_contact(_request):
        pytest.fail("An unsuitable page URL was sent to Wayback")

    with pytest.raises(CollectionFailure):
        lookup_wayback(url, transport=httpx.MockTransport(never_contact))


def test_local_dns_answer_is_rejected_before_sending_to_archive():
    def never_contact(_request):
        pytest.fail("A local DNS answer was sent to Wayback")

    with pytest.raises(CollectionFailure, match="public address"):
        lookup_wayback(
            "https://intranet.example/",
            transport=httpx.MockTransport(never_contact),
            resolver=lambda _hostname: ["93.184.216.34", "192.168.1.3"],
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"url": ARCHIVE.replace("web.archive.org", "example.com")},
        {"url": ARCHIVE.replace("www.nasa.gov", "example.com")},
        {"url": ARCHIVE + "?token=secret"},
        {"timestamp": "20250102231552"},
        {"status": "404"},
    ],
)
def test_lookup_refuses_untrusted_or_mismatched_capture_urls(overrides):
    snapshots = {"closest": {**CAPTURE, **overrides}}
    with pytest.raises(CollectionFailure, match="invalid or mismatched capture"):
        lookup(httpx.Response(200, json={"archived_snapshots": snapshots}))


def test_rate_limit_and_malformed_response_are_not_empty_results():
    with pytest.raises(CollectionFailure, match="HTTP 429"):
        lookup(httpx.Response(429))
    with pytest.raises(CollectionFailure, match="unreadable availability response"):
        lookup(httpx.Response(200, json={"error": "Try later"}))


def test_availability_redirect_is_never_followed():
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://example.com/"})

    with pytest.raises(CollectionFailure, match="could not be completed"):
        lookup_wayback(
            PAGE,
            transport=httpx.MockTransport(respond),
            resolver=lambda _hostname: ["93.184.216.34"],
        )
    assert len(calls) == 1
