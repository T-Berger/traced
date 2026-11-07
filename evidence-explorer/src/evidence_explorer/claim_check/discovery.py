"""Bounded external discovery; result snippets are leads, never saved evidence.

The default reads DuckDuckGo's public non-JavaScript Lite result page. This is a
bounded discovery adapter, not a contracted API: availability, markup, ranking,
coverage and relevance are not guaranteed. Query echo is checked; challenge pages
are rejected without retries or bypass. Missing results remain explicit issues.
Official non-JavaScript variants: https://duckduckgo.com/duckduckgo-help-pages/features/non-javascript

The earlier Bing RSS adapter remains available for explicit tests/injection only.
Live checks found unrelated results, and its feed restricts reuse to personal,
non-commercial RSS aggregation. It is therefore not selected by default.

Production queries run in a short-lived subprocess. Its deadline includes DNS,
HTTP redirects and parsing; a stuck resolver cannot leave a background thread or
unbounded search job. Injected mock transports/resolvers run in process solely for
deterministic tests, and are responsible for their own bounded execution.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import feedparser
import httpx

from ..ingestion.fetching import PublicFetcher
from ..ingestion.models import CollectionError
from ..ingestion.parsing import clean_metadata
from ..ingestion.url_policy import Resolver, normalize_url
from .contracts import Issue, SearchBatch, SearchHit

_SEARCH_WORKER = """
import json, sys
from evidence_explorer.claim_check.discovery import (
    BingRSSSearchProvider, DuckDuckGoLiteSearchProvider,
)
request = json.load(sys.stdin)
provider_type = {'bing_rss': BingRSSSearchProvider, 'duckduckgo_lite': DuckDuckGoLiteSearchProvider}
provider = provider_type[request['provider']](timeout=request['timeout'])
result = provider._search(request['query'], limit=request['limit'])
sys.stdout.write(result.model_dump_json())
"""


class SearchProvider(Protocol):
    def search(self, query: str, *, limit: int = 5) -> SearchBatch:
        """Return discovery leads and explicit errors for one bounded query."""
        ...


class _BoundedSearchProvider:
    name: str

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver | None = None,
        timeout: float = 12.0,
    ):
        self._transport = transport
        self._resolver = resolver
        if not 0 < timeout <= 60:
            raise ValueError("Search timeout must be greater than zero and at most 60 seconds.")
        self._timeout = timeout

    def search(self, query: str, *, limit: int = 5) -> SearchBatch:
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 1000:
            raise ValueError("Search query must contain 1–1,000 characters.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8:
            raise ValueError("Search limit must be between 1 and 8.")
        query = query.strip()
        if self._transport is not None or self._resolver is not None:
            return self._search(query, limit=limit)
        try:
            completed = subprocess.run(
                [sys.executable, "-c", _SEARCH_WORKER],
                input=json.dumps(
                    {
                        "query": query,
                        "limit": limit,
                        "timeout": self._timeout,
                        "provider": self.name,
                    }
                ),
                text=True,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
            if completed.returncode != 0:
                return self._failure(
                    query, "search_worker_error", "External search process failed."
                )
            return SearchBatch.model_validate_json(completed.stdout)
        except subprocess.TimeoutExpired:
            return self._failure(query, "timeout", "External search exceeded its total time limit.")
        except (OSError, ValueError):
            return self._failure(
                query, "search_worker_error", "External search returned an invalid result."
            )

    def _failure(self, query: str, code: str, message: str) -> SearchBatch:
        return SearchBatch(
            query=query,
            provider=self.name,
            searched_at=datetime.now(UTC).isoformat(),
            issues=[Issue(code=code, message=message, query=query)],
        )


class BingRSSSearchProvider(_BoundedSearchProvider):
    """Legacy RSS adapter; not used by the default product discovery path."""

    name = "bing_rss"

    def _search(self, query: str, *, limit: int) -> SearchBatch:
        """Worker implementation; production callers use the bounded search method."""
        searched_at = datetime.now(UTC).isoformat()
        url = "https://www.bing.com/search?" + urlencode({"q": query, "format": "rss"})
        fetcher = PublicFetcher(
            transport=self._transport,
            resolver=self._resolver,
            timeout=self._timeout,
            max_bytes=500_000,
            max_redirects=3,
        )
        hits: list[SearchHit] = []
        issues: list[Issue] = []
        try:
            resource = fetcher.fetch(url)
            if resource.content_type in {"text/html", "application/xhtml+xml"}:
                raise CollectionError(
                    "Search returned an HTML page instead of RSS; "
                    "the provider may be unavailable or require interactive access.",
                    code="invalid_search_response",
                    url=resource.final_url,
                )
            parsed = feedparser.parse(resource.body)
            if parsed.bozo or not parsed.version:
                raise CollectionError(
                    "Search did not return a valid RSS/Atom feed; no leads were accepted.",
                    code="invalid_search_response",
                    url=resource.final_url,
                )
            seen: set[str] = set()
            for rank, entry in enumerate(parsed.entries[:100], start=1):
                if len(hits) >= limit:
                    break
                raw_url = entry.get("link", "")
                try:
                    result_url = normalize_url(raw_url)
                except CollectionError as exc:
                    issues.append(Issue(code=exc.code, message=str(exc), url=raw_url, query=query))
                    continue
                if result_url in seen:
                    continue
                seen.add(result_url)
                hits.append(
                    SearchHit(
                        url=result_url,
                        title=clean_metadata(entry.get("title"), 500) or result_url,
                        snippet=clean_metadata(entry.get("summary"), 2000),
                        rank=rank,
                        query=query,
                        provider=self.name,
                    )
                )
            if not hits:
                issues.append(
                    Issue(
                        code="search_empty",
                        message="The provider returned no usable results for this query.",
                        query=query,
                    )
                )
        except CollectionError as exc:
            issues.append(Issue(code=exc.code, message=str(exc), url=exc.url or url, query=query))
        finally:
            fetcher.close()
        return SearchBatch(
            query=query, provider=self.name, searched_at=searched_at, hits=hits, issues=issues
        )


class _LiteResultsParser(HTMLParser):
    """Read visible result links and snippets, never scripts or embedded commands."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.query_values: list[str] = []
        self.results: list[dict[str, str]] = []
        self._result: dict[str, str] | None = None
        self._snippet = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if tag == "input" and values.get("name") == "q":
            self.query_values.append(values.get("value") or "")
        elif tag == "a" and "result-link" in classes:
            self._result = {"url": values.get("href") or "", "title": "", "snippet": ""}
        elif tag == "td" and "result-snippet" in classes:
            self._snippet = True

    def handle_data(self, data: str) -> None:
        if self._result is not None:
            self._result["title"] += data
        elif self._snippet and self.results:
            self.results[-1]["snippet"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._result is not None:
            self.results.append(self._result)
            self._result = None
        elif tag == "td":
            self._snippet = False


def _lite_destination(raw_url: str) -> str:
    """Decode the provider's result wrapper, then apply public URL validation."""
    absolute = urljoin("https://lite.duckduckgo.com/", raw_url)
    parsed = urlsplit(absolute)
    if parsed.hostname == "duckduckgo.com" or (parsed.hostname or "").endswith(".duckduckgo.com"):
        targets = parse_qs(parsed.query).get("uddg", [])
        if parsed.path != "/l/" or len(targets) != 1:
            raise CollectionError(
                "Search result did not identify an external publisher URL.",
                code="invalid_search_link",
                url=absolute,
            )
        absolute = targets[0]
    return normalize_url(absolute)


class DuckDuckGoLiteSearchProvider(_BoundedSearchProvider):
    """Search the fixed public Lite page; interactive challenges fail visibly."""

    name = "duckduckgo_lite"

    def _search(self, query: str, *, limit: int) -> SearchBatch:
        searched_at = datetime.now(UTC).isoformat()
        url = "https://lite.duckduckgo.com/lite/?" + urlencode({"q": query})
        fetcher = PublicFetcher(
            transport=self._transport,
            resolver=self._resolver,
            timeout=self._timeout,
            max_bytes=500_000,
            max_redirects=3,
        )
        hits: list[SearchHit] = []
        issues: list[Issue] = []
        try:
            resource = fetcher.fetch(url)
            body = resource.body.decode("utf-8", errors="replace")
            if "anomaly.js" in body or 'id="challenge-form"' in body:
                raise CollectionError(
                    "The search provider requires an interactive challenge; "
                    "no results were accepted and no bypass was attempted.",
                    code="search_challenge",
                    url=resource.final_url,
                )
            if resource.content_type not in {"text/html", "application/xhtml+xml"}:
                raise CollectionError(
                    "Search did not return a supported Lite results page.",
                    code="invalid_search_response",
                    url=resource.final_url,
                )
            parsed = _LiteResultsParser()
            parsed.feed(body)
            parsed.close()
            if query not in parsed.query_values:
                raise CollectionError(
                    "Search response did not echo the requested query; no leads were accepted.",
                    code="search_query_mismatch",
                    url=resource.final_url,
                )
            seen: set[str] = set()
            for rank, entry in enumerate(parsed.results[:100], start=1):
                if len(hits) >= limit:
                    break
                try:
                    result_url = _lite_destination(entry["url"])
                except CollectionError as exc:
                    issues.append(Issue(code=exc.code, message=str(exc), url=exc.url, query=query))
                    continue
                if result_url in seen:
                    continue
                seen.add(result_url)
                hits.append(
                    SearchHit(
                        url=result_url,
                        title=clean_metadata(entry["title"], 500) or result_url,
                        snippet=clean_metadata(entry["snippet"], 2000),
                        rank=rank,
                        query=query,
                        provider=self.name,
                    )
                )
            if not hits:
                recognized_empty = "no results found" in body.lower()
                issues.append(
                    Issue(
                        code="search_empty" if recognized_empty else "invalid_search_response",
                        message="The provider returned no results for this query."
                        if recognized_empty
                        else "The search page contained no recognized result links; "
                        "its format may have changed.",
                        query=query,
                    )
                )
        except CollectionError as exc:
            issues.append(Issue(code=exc.code, message=str(exc), url=exc.url or url, query=query))
        finally:
            fetcher.close()
        return SearchBatch(
            query=query, provider=self.name, searched_at=searched_at, hits=hits, issues=issues
        )


def default_search_provider() -> SearchProvider:
    """No secrets, external SDK or browser automation required."""
    return DuckDuckGoLiteSearchProvider()
