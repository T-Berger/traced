"""Bounded HTTP fetching with manual redirects and public-address pinning."""

import time
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin

import httpx

from .models import CollectionError
from .url_policy import Resolver, normalize_url, public_addresses, system_resolver


@dataclass(frozen=True)
class FetchedResource:
    requested_url: str
    final_url: str
    body: bytes
    content_type: str
    retrieved_at: str


class PublicFetcher:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver | None = None,
        timeout: float = 20.0,
        max_bytes: int = 3_000_000,
        max_redirects: int = 5,
    ):
        if timeout <= 0 or max_bytes < 1 or max_redirects < 0:
            raise ValueError("Fetch limits must be positive (redirect count can be zero).")
        self.resolver = resolver or system_resolver
        self.timeout, self.max_bytes, self.max_redirects = timeout, max_bytes, max_redirects
        self._pin_addresses = transport is None
        self.client = httpx.Client(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(timeout),
            # Pinned IPs can host unrelated TLS names. Never reuse a connection
            # across logical hosts simply because they share that public IP.
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            headers={
                "User-Agent": "EvidenceExplorer/0.1 (public-news research collector)",
                "Accept": (
                    "text/html, application/xhtml+xml, application/rss+xml, "
                    "application/atom+xml, application/xml, text/xml;q=0.9"
                ),
                "Accept-Encoding": "identity",
            },
        )

    def close(self) -> None:
        self.client.close()

    def _read(self, response: httpx.Response, url: str, started: float) -> bytes:
        declared = response.headers.get("content-length")
        if declared:
            try:
                declared_size = int(declared)
            except ValueError as exc:
                raise CollectionError(
                    "Source sent an invalid Content-Length.", code="http_error", url=url
                ) from exc
            if declared_size < 0:
                raise CollectionError(
                    "Source sent an invalid Content-Length.", code="http_error", url=url
                )
            if declared_size > self.max_bytes:
                raise CollectionError(
                    "Response exceeds the download size limit.",
                    code="response_too_large",
                    url=url,
                )
        # Mock transports can supply already consumed responses; production uses raw streaming.
        if response.is_stream_consumed:
            if len(response.content) > self.max_bytes:
                raise CollectionError(
                    "Response exceeds the download size limit.", code="response_too_large", url=url
                )
            return response.content
        encoding = response.headers.get("content-encoding", "identity").lower().strip()
        if encoding in {"", "identity"}:
            decoder = None
        elif encoding in {"gzip", "x-gzip", "deflate"}:
            decoder = zlib.decompressobj(
                16 + zlib.MAX_WBITS if encoding != "deflate" else zlib.MAX_WBITS
            )
        else:
            raise CollectionError(
                "Source returned an unsupported content encoding.",
                code="unsupported_content",
                url=url,
            )
        parts, total, wire_total = [], 0, 0
        for block in response.iter_raw(chunk_size=65_536):
            wire_total += len(block)
            if time.monotonic() - started > self.timeout:
                raise CollectionError(
                    "Source download exceeded the time limit.", code="timeout", url=url
                )
            if wire_total > self.max_bytes:
                raise CollectionError(
                    "Response exceeds the download size limit.", code="response_too_large", url=url
                )
            try:
                decoded = (
                    decoder.decompress(block, self.max_bytes - total + 1) if decoder else block
                )
            except zlib.error as exc:
                raise CollectionError(
                    "Source returned invalid compressed content.", code="http_error", url=url
                ) from exc
            total += len(decoded)
            if total > self.max_bytes or (decoder and decoder.unconsumed_tail):
                raise CollectionError(
                    "Expanded response exceeds the download size limit.",
                    code="response_too_large",
                    url=url,
                )
            if decoder and decoder.unused_data:
                raise CollectionError(
                    "Source returned trailing or concatenated compressed content.",
                    code="unsupported_content",
                    url=url,
                )
            parts.append(decoded)
        if decoder and not decoder.eof:
            raise CollectionError(
                "Source returned incomplete compressed content.", code="http_error", url=url
            )
        return b"".join(parts)

    def fetch(self, url: str) -> FetchedResource:
        requested_url, current = url.strip(), normalize_url(url)
        started = time.monotonic()
        for redirect in range(self.max_redirects + 1):
            addresses = public_addresses(current, self.resolver)
            logical = httpx.URL(current)
            # TLS verifies the original hostname while the socket connects to a vetted
            # literal. This prevents a second DNS lookup from rebinding to a private IP.
            target = logical.copy_with(host=addresses[0]) if self._pin_addresses else logical
            extensions = {"sni_hostname": logical.host} if self._pin_addresses else {}
            remaining = self.timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise CollectionError(
                    "Source download exceeded the time limit.", code="timeout", url=current
                )
            try:
                with self.client.stream(
                    "GET",
                    target,
                    headers={"Host": logical.netloc.decode("ascii")},
                    extensions=extensions,
                    timeout=remaining,
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise CollectionError(
                                "Redirect has no destination.", code="http_error", url=current
                            )
                        if redirect >= self.max_redirects:
                            raise CollectionError(
                                "Source exceeded the redirect limit.",
                                code="redirect_limit",
                                url=current,
                            )
                        current = normalize_url(urljoin(current, location))
                        continue
                    if not 200 <= response.status_code < 300:
                        raise CollectionError(
                            f"Source returned HTTP {response.status_code}; "
                            "no article was imported.",
                            code="http_error",
                            url=current,
                        )
                    body = self._read(response, current, started)
                    return FetchedResource(
                        requested_url=requested_url,
                        final_url=current,
                        body=body,
                        content_type=response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower(),
                        retrieved_at=datetime.now(UTC).isoformat(),
                    )
            except httpx.TimeoutException as exc:
                raise CollectionError(
                    "Source request timed out.", code="timeout", url=current
                ) from exc
            except httpx.HTTPError as exc:
                raise CollectionError(
                    f"Source request failed: {exc}", code="network_error", url=current
                ) from exc
        raise CollectionError(
            "Source exceeded the redirect limit.", code="redirect_limit", url=current
        )
