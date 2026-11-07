"""Bounded, read-only connection check for a configured Aleph instance."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx

from ..ingestion.fetching import PublicFetcher
from ..ingestion.models import CollectionError
from ..ingestion.url_policy import Resolver, normalize_url

_OCCRP_ORIGIN = "https://aleph.occrp.org"
_COLLECTIONS_PATH = "/api/2/collections?limit=1"
_HTTP_STATUS = re.compile(r"^Source returned HTTP ([1-5]\d{2}); ")


@dataclass(frozen=True)
class AlephConfiguration:
    instance_url: str | None
    ready: bool
    message: str


@dataclass(frozen=True)
class AlephConnectionResult:
    instance_url: str | None
    status: Literal["setup_required", "connected", "provider_unavailable"]
    message: str
    http_status: int | None = None


def _settings() -> tuple[AlephConfiguration, str]:
    raw_origin = os.environ.get("TRACED_ALEPH_BASE_URL", "").strip()
    if not raw_origin:
        return AlephConfiguration(
            _OCCRP_ORIGIN,
            False,
            "Set the server-side Aleph instance URL and API key to connect.",
        ), ""

    try:
        parsed = urlsplit(raw_origin)
        if (
            parsed.scheme != "https"
            or parsed.path not in {"", "/"}
            or "?" in raw_origin
            or "#" in raw_origin
        ):
            raise ValueError("An HTTPS origin is required.")
        origin = normalize_url(raw_origin).rstrip("/")
    except ValueError:
        return AlephConfiguration(
            None,
            False,
            "Set the Aleph instance to a public HTTPS origin without a path or credentials.",
        ), ""

    key = os.environ.get("TRACED_ALEPH_API_KEY", "").strip(" ")
    # Aleph keys may contain a space between the key ID and the secret.
    if not key or len(key) > 4096 or any(not 32 <= ord(char) < 127 for char in key):
        return AlephConfiguration(
            origin, False, "Set a valid server-side Aleph API key to connect."
        ), ""
    return AlephConfiguration(origin, True, "Aleph settings are ready to check."), key


def aleph_configuration() -> AlephConfiguration:
    """Describe setup without making a request or returning a credential."""
    return _settings()[0]


def _failure(origin: str, exc: CollectionError) -> AlephConnectionResult:
    match = _HTTP_STATUS.match(str(exc)) if exc.code == "http_error" else None
    status = int(match.group(1)) if match else None
    if status == 401:
        return AlephConnectionResult(
            origin,
            "setup_required",
            "Aleph rejected the API key. Check the server settings.",
            status,
        )
    if status == 403:
        message = "Aleph denied access to collections for this key."
    elif status == 429:
        message = "Aleph is rate limiting requests. Try the connection check later."
    elif exc.code == "redirect_limit":
        message = "Aleph redirected the request. Set the final HTTPS origin and try again."
    elif exc.code == "blocked_url":
        message = "The Aleph instance did not resolve to a public network address."
    elif exc.code == "timeout":
        message = "The Aleph connection check timed out."
    elif status is not None:
        message = f"Aleph returned HTTP {status} during the connection check."
    else:
        message = "Aleph could not be checked. Verify the instance URL and try again."
    return AlephConnectionResult(origin, "provider_unavailable", message, status)


def check_aleph_connection(
    *, transport: httpx.BaseTransport | None = None, resolver: Resolver | None = None
) -> AlephConnectionResult:
    """Check collection-list access with one authenticated GET to the configured origin."""
    configuration, key = _settings()
    origin = configuration.instance_url
    if not configuration.ready or origin is None:
        return AlephConnectionResult(origin, "setup_required", configuration.message)

    fetcher = PublicFetcher(
        transport=transport, resolver=resolver, timeout=8.0, max_bytes=131_072, max_redirects=0
    )
    # The dedicated fetcher sends credentials to one origin and follows no redirects.
    fetcher.client.headers.update({"Accept": "application/json", "Authorization": "ApiKey " + key})
    try:
        resource = fetcher.fetch(origin + _COLLECTIONS_PATH)
    except CollectionError as exc:
        return _failure(origin, exc)
    finally:
        fetcher.close()

    try:
        if resource.content_type != "application/json":
            raise ValueError("Expected JSON.")
        data = json.loads(resource.body)
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
            raise ValueError("Expected collection results.")
    except (ValueError, RecursionError):
        return AlephConnectionResult(
            origin, "provider_unavailable", "Aleph returned an invalid collection list."
        )
    return AlephConnectionResult(origin, "connected", "Aleph collection-list access verified.", 200)
