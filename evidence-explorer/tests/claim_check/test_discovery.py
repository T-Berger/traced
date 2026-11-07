"""Search discovery never promotes snippets, blocked URLs or error pages to evidence."""

import time
from html import escape as html_escape
from urllib.parse import urlencode
from xml.sax.saxutils import escape

import httpx
import pytest

from evidence_explorer.claim_check.discovery import (
    BingRSSSearchProvider,
    DuckDuckGoLiteSearchProvider,
    default_search_provider,
)


def rss(*urls):
    items = "".join(
        f"<item><title>Result {i}</title><link>{escape(url)}</link>"
        "<description>&lt;b&gt;A search teaser&lt;/b&gt;</description></item>"
        for i, url in enumerate(urls)
    )
    return (
        '<rss version="2.0"><channel><title>Search</title><link>https://www.bing.com/</link>'
        f"<description>Search results</description>{items}</channel></rss>"
    )


def provider(handler):
    return BingRSSSearchProvider(
        transport=httpx.MockTransport(handler), resolver=lambda host: ["93.184.216.34"]
    )


def test_search_preserves_query_and_returns_bounded_unique_leads():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            text=rss(
                "https://news.example/a?utm_source=rss",
                "https://news.example/a#duplicate",
                "https://news.example/b",
                "https://news.example/c",
            ),
            headers={"content-type": "application/rss+xml"},
        )

    result = provider(respond).search(" bridge & safety? ", limit=2)
    assert requests[0].url.params["q"] == "bridge & safety?"
    assert requests[0].url.params["format"] == "rss"
    assert [hit.url for hit in result.hits] == ["https://news.example/a", "https://news.example/b"]
    assert result.hits[1].rank == 3
    assert result.hits[0].snippet == "A search teaser"
    assert result.hits[0].provider == result.provider == "bing_rss"
    assert result.searched_at
    assert not result.issues


def test_private_result_url_is_reported_and_never_returned_as_lead():
    result = provider(
        lambda request: httpx.Response(
            200, text=rss("http://127.0.0.1/private", "https://news.example/article")
        )
    ).search("test")
    assert [hit.url for hit in result.hits] == ["https://news.example/article"]
    assert result.issues[0].code == "blocked_url"


@pytest.mark.parametrize("body", ["<html>CAPTCHA challenge</html>", "<rss><channel><item>"])
def test_block_pages_and_malformed_responses_are_not_empty_successes(body):
    result = provider(lambda request: httpx.Response(200, text=body)).search("test")
    assert not result.hits
    assert result.issues[0].code == "invalid_search_response"


def test_valid_empty_feed_records_a_completed_search_without_inventing_results():
    result = provider(lambda request: httpx.Response(200, text=rss())).search("unusual query")
    assert result.hits == []
    assert result.issues[0].code == "search_empty"


def test_html_response_is_rejected_even_when_it_embeds_feed_markup():
    result = provider(
        lambda request: httpx.Response(
            200, text=rss("https://news.example/a"), headers={"content-type": "text/html"}
        )
    ).search("test")
    assert result.hits == []
    assert result.issues[0].code == "invalid_search_response"


def test_production_deadline_terminates_a_stuck_search_process(monkeypatch):
    monkeypatch.setattr(
        "evidence_explorer.claim_check.discovery._SEARCH_WORKER", "import time; time.sleep(10)"
    )
    started = time.monotonic()
    result = BingRSSSearchProvider(timeout=0.15).search("stuck DNS simulation")
    assert time.monotonic() - started < 2
    assert result.issues[0].code == "timeout"
    assert result.hits == []


def test_worker_failure_is_visible_without_exposing_stderr(monkeypatch):
    monkeypatch.setattr(
        "evidence_explorer.claim_check.discovery._SEARCH_WORKER",
        "import sys; sys.stderr.write('sensitive diagnostic'); sys.exit(1)",
    )
    result = BingRSSSearchProvider().search("test")
    assert result.issues[0].code == "search_worker_error"
    assert "sensitive diagnostic" not in result.model_dump_json()


def test_search_failure_is_explicit_and_does_not_retry_challenge_pages():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(403, text="blocked")

    result = provider(respond).search("test")
    assert len(requests) == 1
    assert result.hits == []
    assert result.issues[0].code == "http_error"
    assert "403" in result.issues[0].message


@pytest.mark.parametrize("query,limit", [("", 5), ("x" * 1001, 5), ("ok", 0), ("ok", True)])
def test_search_rejects_unbounded_inputs_before_request(query, limit):
    def unexpected(request):
        pytest.fail("Invalid input must not make a network request")

    with pytest.raises(ValueError):
        provider(unexpected).search(query, limit=limit)


def lite_page(query, *urls):
    results = "".join(
        '<tr><td><a class="result-link" href="'
        + html_escape("//duckduckgo.com/l/?" + urlencode({"uddg": url}))
        + '">NASA <b>Webb</b> report</a></td></tr>'
        '<tr><td class="result-snippet">A <b>discovery</b> teaser</td></tr>'
        for url in urls
    )
    return f'<html><input name="q" value="{html_escape(query)}"><table>{results}</table></html>'


def lite_provider(handler):
    return DuckDuckGoLiteSearchProvider(
        transport=httpx.MockTransport(handler), resolver=lambda host: ["93.184.216.34"]
    )


def test_default_is_lite_provider_not_unreliable_bing_rss():
    assert isinstance(default_search_provider(), DuckDuckGoLiteSearchProvider)


def test_lite_search_decodes_publisher_urls_and_preserves_exact_query():
    requests = []
    query = '"TWA 7" & Webb'

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            text=lite_page(query, "https://science.nasa.gov/a", "https://esawebb.org/b"),
            headers={"content-type": "text/html"},
        )

    result = lite_provider(respond).search(query, limit=1)
    assert requests[0].url.params["q"] == query
    assert requests[0].url.host == "lite.duckduckgo.com"
    assert result.hits[0].url == "https://science.nasa.gov/a"
    assert result.hits[0].title == "NASA Webb report"
    assert result.hits[0].snippet == "A discovery teaser"
    assert len(result.hits) == len(requests) == 1
    assert result.issues == []


def test_lite_search_rejects_mismatched_query_echo():
    result = lite_provider(
        lambda request: httpx.Response(
            200,
            text=lite_page("different query", "https://news.example/a"),
            headers={"content-type": "text/html"},
        )
    ).search("TWA 7")
    assert not result.hits
    assert result.issues[0].code == "search_query_mismatch"


def test_lite_search_never_retries_or_accepts_challenge_results():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            202,
            text=lite_page("TWA 7", "https://news.example/a")
            + '<form id="challenge-form" action="/anomaly.js"></form>',
            headers={"content-type": "text/html"},
        )

    result = lite_provider(respond).search("TWA 7")
    assert not result.hits
    assert len(requests) == 1
    assert result.issues[0].code == "search_challenge"


@pytest.mark.parametrize(
    "suffix,expected",
    [
        ("<p>No results found</p>", "search_empty"),
        ("<p>New result format</p>", "invalid_search_response"),
    ],
)
def test_lite_search_distinguishes_empty_results_from_unrecognized_markup(suffix, expected):
    result = lite_provider(
        lambda request: httpx.Response(
            200,
            text=lite_page("TWA 7") + suffix,
            headers={"content-type": "text/html"},
        )
    ).search("TWA 7")
    assert not result.hits
    assert result.issues[0].code == expected


def test_lite_redirect_wrapper_cannot_smuggle_a_private_url():
    result = lite_provider(
        lambda request: httpx.Response(
            200,
            text=lite_page("TWA 7", "http://127.0.0.1/private", "https://news.example/a"),
            headers={"content-type": "text/html"},
        )
    ).search("TWA 7")
    assert len(result.hits) == 1
    assert result.hits[0].url == "https://news.example/a"
    assert result.issues[0].code == "blocked_url"
