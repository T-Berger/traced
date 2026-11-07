"""Evidence records hold fetched text and provenance, even when other URLs fail."""

import hashlib

import httpx

from evidence_explorer.claim_check.collection import collect_evidence
from evidence_explorer.claim_check.contracts import SearchHit
from evidence_explorer.ingestion.collector import NewsCollector

ARTICLE = """<html lang="en"><head><title>Bridge inspection report</title>
<meta property="og:site_name" content="Example Authority">
<meta name="author" content="Example Engineer"></head><body><main><article>
<h1>Bridge inspection report</h1>
<p>The bridge remains closed to vehicles while engineers inspect the river foundations.
The council has not announced a reopening date, and pedestrians must use the alternate route.</p>
<p>Inspectors recorded the water level and checked structural elements on Tuesday.
The authority will publish a further report when the review has been completed.</p>
</article></main></body></html>"""


def collector(handler):
    return NewsCollector(
        transport=httpx.MockTransport(handler), resolver=lambda host: ["93.184.216.34"]
    )


def lead(url, *, snippet="A deliberately misleading search teaser"):
    return SearchHit(url=url, title="Unverified discovery title", snippet=snippet, query="bridge")


def test_saved_evidence_uses_only_fetched_body_and_keeps_unknown_dates_unknown():
    with collector(
        lambda request: httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"})
    ) as client:
        result = collect_evidence([lead("https://news.example/article")], collector=client)
    assert not result.issues
    record = result.records[0]
    assert "bridge remains closed" in record.text
    assert "misleading search teaser" not in record.text
    assert record.title == "Bridge inspection report"
    assert record.publisher == "Example Authority"
    assert record.published_at is None
    assert record.retrieved_at
    assert record.content_hash == hashlib.sha256(record.text.encode()).hexdigest()
    assert record.metadata["provenance"]["author"] == "Example Engineer"
    assert record.metadata["discovery_only"]["snippet"].endswith("search teaser")
    assert record.metadata["independence"] == "unreviewed"


def test_failed_attempts_consume_budget_and_never_fall_back_to_snippets():
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if request.url.path == "/failed":
            return httpx.Response(403, text="blocked")
        return httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"})

    with collector(respond) as client:
        result = collect_evidence(
            [lead(f"https://news.example/{path}") for path in ["failed", "valid", "over-budget"]],
            collector=client,
            limit=2,
        )
    assert len(requests) == len(result.attempted_urls) == 2
    assert len(result.records) == 1
    assert result.issues[0].code == "http_error"
    assert result.issues[0].url == "https://news.example/failed"


def test_canonical_duplicate_leads_are_not_fetched_twice():
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"})

    with collector(respond) as client:
        result = collect_evidence(
            [lead("https://news.example/a?utm_source=one"), lead("https://news.example/a#two")],
            collector=client,
        )
    assert len(requests) == len(result.records) == 1


def test_redirects_preserve_requested_and_actual_source_urls():
    def respond(request):
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "/new"})
        return httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"})

    with collector(respond) as client:
        result = collect_evidence([lead("https://news.example/old")], collector=client)
    record = result.records[0]
    assert record.requested_url == "https://news.example/old"
    assert record.url == "https://news.example/new"


def test_private_sources_are_rejected_before_transport_and_count_as_attempts():
    requests = []
    with collector(lambda request: requests.append(request)) as client:
        result = collect_evidence(
            [lead("http://127.0.0.1/private"), lead("https://news.example/unused")],
            collector=client,
            limit=1,
        )
    assert not requests
    assert not result.records
    assert result.attempted_urls == ["http://127.0.0.1/private"]
    assert result.issues[0].code == "blocked_url"


def test_same_text_at_different_publishers_is_preserved_for_independence_review():
    with collector(
        lambda request: httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"})
    ) as client:
        result = collect_evidence(
            [lead("https://first.example/a"), lead("https://second.example/a")], collector=client
        )
    assert len(result.records) == 2
    first, second = result.records
    assert first.content_hash == second.content_hash
    assert first.id != second.id


def test_changed_content_receives_a_new_stable_evidence_identity():
    identities = []
    for text in [ARTICLE, ARTICLE, ARTICLE.replace("remains closed", "has reopened")]:
        with collector(
            lambda request, body=text: httpx.Response(
                200, text=body, headers={"content-type": "text/html"}
            )
        ) as client:
            identities.append(
                collect_evidence([lead("https://news.example/a")], collector=client).records[0].id
            )
    assert identities[0] == identities[1]
    assert identities[2] != identities[0]
