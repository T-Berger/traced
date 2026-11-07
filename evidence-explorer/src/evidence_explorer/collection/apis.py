"""Read-only official X and Reddit adapters with server-side credentials.

Requests use fixed platform endpoints, public-address pinning, bounded downloads,
and no redirects. API failures never contain provider response bodies or tokens.
Reddit app-only OAuth follows its documented client_credentials grant; approval
and endpoint access are still controlled by the platform.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from ..ingestion.url_policy import Resolver, public_addresses, system_resolver
from ..source_contracts import SourceIdentity, SourceItem, safe_source_url
from .models import CollectedBatch, CollectionFailure, CollectionSource
from .registry import normalize_locator

MAX_API_BYTES = 3_000_000
API_TIMEOUT_SECONDS = 20.0
_X_ID = re.compile(r"[0-9]{1,30}")
_REDDIT_ID = re.compile(r"[A-Za-z0-9]{1,30}")
_DEFAULT_AGENT = "python:local.evidence-explorer:v0.1.0 (read-only research collector)"
_API_HOSTS = {"api.x.com", "oauth.reddit.com", "www.reddit.com"}


def _setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    # Do not allow invalid header values or accidental pasted multiline secrets.
    return value if value and all(32 < ord(char) < 127 for char in value) else ""


def configuration_status(platform: str) -> dict:
    """Return configuration presence, never a token or a claim of API approval."""
    if platform == "x":
        ready = bool(_setting("X_BEARER_TOKEN"))
        return {
            "ready": ready,
            "message": (
                "X token configured; collection depends on your API access."
                if ready
                else "Connect X by configuring its server-side bearer token."
            ),
        }
    if platform == "reddit":
        ready = bool(
            _setting("REDDIT_ACCESS_TOKEN")
            or (_setting("REDDIT_CLIENT_ID") and _setting("REDDIT_CLIENT_SECRET"))
        )
        return {
            "ready": ready,
            "message": (
                "Reddit credentials configured; collection depends on approved API access."
                if ready
                else "Connect Reddit with an approved access token or app credentials."
            ),
        }
    return {"ready": False, "message": "Choose X or Reddit for this connection."}


def _retry_after(headers: httpx.Headers) -> int:
    raw = headers.get("retry-after", "")
    try:
        delay = float(raw)
        if math.isfinite(delay) and delay >= 0:
            return min(86400, max(1, math.ceil(delay)))
    except ValueError:
        try:
            deadline = parsedate_to_datetime(raw)
            if deadline.tzinfo is not None:
                return min(86400, max(1, math.ceil(deadline.timestamp() - time.time())))
        except (ValueError, TypeError, OverflowError):
            pass
    try:
        deadline = float(headers.get("x-rate-limit-reset", ""))
        if math.isfinite(deadline):
            return min(86400, max(1, math.ceil(deadline - time.time())))
    except (ValueError, OverflowError):
        pass
    return 60


class _APIClient:
    def __init__(self, platform: str, transport=None, resolver: Resolver | None = None):
        self.label = "X" if platform == "x" else "Reddit"
        self.resolver = resolver or system_resolver
        self.pin_addresses = transport is None
        self.client = httpx.Client(
            transport=transport,
            timeout=API_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.client.close()

    def request(self, method: str, url: str, *, headers: dict, params=None, data=None, auth=None):
        logical = httpx.URL(url)
        if (
            logical.scheme != "https"
            or logical.host not in _API_HOSTS
            or logical.port not in {None, 443}
            or logical.userinfo
        ):
            raise CollectionFailure("The collector refused an unexpected API destination.")
        target, extensions = logical, {}
        if self.pin_addresses:
            try:
                addresses = public_addresses(url, self.resolver)
            except Exception:
                raise CollectionFailure(f"{self.label} could not be reached safely.") from None
            target = logical.copy_with(host=addresses[0])
            extensions = {"sni_hostname": logical.host}
        started = time.monotonic()
        try:
            with self.client.stream(
                method,
                target,
                headers={**headers, "Host": logical.netloc.decode("ascii")},
                params=params,
                data=data,
                auth=auth,
                extensions=extensions,
            ) as response:
                self._check_status(response)
                try:
                    declared = int(response.headers.get("content-length", "0"))
                except ValueError:
                    raise CollectionFailure(
                        f"{self.label} returned an invalid response size."
                    ) from None
                if not 0 <= declared <= MAX_API_BYTES:
                    raise CollectionFailure(f"{self.label} response exceeded the download limit.")
                if response.headers.get("content-encoding", "identity").lower() not in {
                    "identity",
                    "",
                }:
                    raise CollectionFailure(f"{self.label} returned an unsupported encoding.")
                parts, size = [], 0
                for chunk in response.iter_bytes(chunk_size=65_536):
                    if time.monotonic() - started > API_TIMEOUT_SECONDS:
                        raise CollectionFailure(f"{self.label} request timed out; try again later.")
                    size += len(chunk)
                    if size > MAX_API_BYTES:
                        raise CollectionFailure(
                            f"{self.label} response exceeded the download limit."
                        )
                    parts.append(chunk)
                try:
                    payload = json.loads(b"".join(parts))
                except (ValueError, UnicodeError, RecursionError):
                    raise CollectionFailure(f"{self.label} returned invalid API data.") from None
                if not isinstance(payload, dict):
                    raise CollectionFailure(f"{self.label} returned invalid API data.")
                return payload
        except httpx.TimeoutException:
            raise CollectionFailure(f"{self.label} request timed out; try again later.") from None
        except (httpx.HTTPError, UnicodeError, ValueError) as exc:
            if isinstance(exc, CollectionFailure):
                raise
            raise CollectionFailure(f"{self.label} request failed; try again later.") from None

    def _check_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        if status == 429:
            raise CollectionFailure(
                f"{self.label} rate limit reached. Collection will wait before retrying.",
                retry_after_seconds=_retry_after(response.headers),
            )
        if status == 401:
            message = (
                f"{self.label} rejected the credentials. "
                "Reconnect the account or refresh the token."
            )
        elif status in {402, 403}:
            message = (
                f"{self.label} denied API access. Check your app approval and endpoint access."
            )
        elif 300 <= status < 400:
            message = f"{self.label} redirected the API request; no credentials were forwarded."
        elif status == 404:
            message = f"{self.label} could not find this source, or it is unavailable to the app."
        else:
            message = f"{self.label} returned HTTP {status}; no records were accepted."
        raise CollectionFailure(message)


def _urls(values: list[Any]) -> list[str]:
    result = []
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            url = safe_source_url(value)
        except ValueError:
            continue
        if url not in result:
            result.append(url)
        if len(result) >= 100:
            break
    return result


def _x(source: CollectionSource, client: _APIClient) -> CollectedBatch:
    username = normalize_locator("x", source.locator)
    token = _setting("X_BEARER_TOKEN")
    if not token:
        raise CollectionFailure(configuration_status("x")["message"])
    headers = {"Authorization": "Bearer " + token, "User-Agent": _DEFAULT_AGENT}
    lookup = client.request(
        "GET", f"https://api.x.com/2/users/by/username/{username}", headers=headers
    )
    user = lookup.get("data")
    if (
        not isinstance(user, dict)
        or not isinstance(user.get("id"), str)
        or not _X_ID.fullmatch(user["id"])
    ):
        raise CollectionFailure("X did not return an accessible source account.")
    user_id = user["id"]
    endpoint = f"https://api.x.com/2/users/{user_id}/tweets"
    params = {
        "max_results": max(5, source.item_limit),
        "tweet.fields": "created_at,author_id,entities,referenced_tweets",
        "expansions": "referenced_tweets.id",
    }
    if source.cursor:
        if not _X_ID.fullmatch(source.cursor):
            raise CollectionFailure(
                "The saved X cursor is invalid. Re-add this source to reset it."
            )
        params["since_id"] = source.cursor
    payload = client.request("GET", endpoint, headers=headers, params=params)
    posts = payload.get("data", [])
    if not isinstance(posts, list):
        raise CollectionFailure("X returned invalid post data.")
    if payload.get("errors") and not posts:
        raise CollectionFailure("X did not return accessible posts. Check the app's source access.")
    includes = payload.get("includes", {})
    included_posts = includes.get("tweets", []) if isinstance(includes, dict) else []
    originals = (
        {
            post.get("id"): post
            for post in included_posts
            if isinstance(post, dict) and isinstance(post.get("id"), str)
        }
        if isinstance(included_posts, list)
        else {}
    )
    acquired = datetime.now(UTC).isoformat()
    items, skipped = [], 0
    for post in posts[: max(5, source.item_limit)]:
        try:
            if (
                not isinstance(post, dict)
                or not isinstance(post.get("id"), str)
                or not _X_ID.fullmatch(post["id"])
            ):
                raise ValueError
            entities = post.get("entities", {})
            raw_urls = entities.get("urls", []) if isinstance(entities, dict) else []
            shared = (
                _urls(
                    [
                        value.get("expanded_url") or value.get("url")
                        for value in raw_urls
                        if isinstance(value, dict)
                    ]
                )
                if isinstance(raw_urls, list)
                else []
            )
            repost = None
            refs = post.get("referenced_tweets", [])
            for ref in refs if isinstance(refs, list) else []:
                if not isinstance(ref, dict) or ref.get("type") != "retweeted":
                    continue
                original = originals.get(ref.get("id"), {})
                author = original.get("author_id")
                if isinstance(author, str) and _X_ID.fullmatch(author):
                    repost = SourceIdentity(platform="x", channel_id=author, native_id=ref["id"])
                break
            items.append(
                SourceItem(
                    platform="x",
                    channel_id=user_id,
                    channel_name=source.label,
                    native_id=post["id"],
                    text=post.get("text"),
                    source_url=f"https://x.com/{username}/status/{post['id']}",
                    author_id=post.get("author_id") or user_id,
                    published_at=post.get("created_at"),
                    acquired_at=acquired,
                    shared_urls=shared,
                    repost_of=repost,
                    provenance={
                        "method": "x_api",
                        "content_scope": "post_text",
                        "retrieved_url": endpoint,
                    },
                )
            )
        except (ValueError, TypeError, ValidationError):
            skipped += 1
    metadata = payload.get("meta", {})
    truncated = len(items) > source.item_limit or (
        isinstance(metadata, dict) and bool(metadata.get("next_token"))
    )
    warnings = []
    if payload.get("errors"):
        warnings.append("X reported partial API errors; some source context may be unavailable.")
    if skipped:
        warnings.append(f"X omitted {skipped} posts with incomplete or invalid source fields.")
    if truncated:
        warnings.append(
            "X reached the per-run collection limit. Older new posts may be outside this window; "
            "the saved cursor has not advanced."
        )
    selected = tuple(items[: source.item_limit])
    cursor = source.cursor
    if selected and not truncated and not skipped:
        cursor = max((item.native_id for item in selected), key=int)
    return CollectedBatch(items=selected, warnings=tuple(warnings), cursor=cursor)


def _reddit_token(client: _APIClient, agent: str) -> str:
    token = _setting("REDDIT_ACCESS_TOKEN")
    if token:
        return token
    app_id, secret = _setting("REDDIT_CLIENT_ID"), _setting("REDDIT_CLIENT_SECRET")
    if not (app_id and secret):
        raise CollectionFailure(configuration_status("reddit")["message"])
    payload = client.request(
        "POST",
        "https://www.reddit.com/api/v1/access_token",
        headers={"User-Agent": agent},
        data={"grant_type": "client_credentials"},
        auth=httpx.BasicAuth(app_id, secret),
    )
    token = payload.get("access_token")
    if (
        not isinstance(token, str)
        or not token
        or not all(32 < ord(char) < 127 for char in token)
        or str(payload.get("token_type", "")).lower() != "bearer"
    ):
        raise CollectionFailure(
            "Reddit did not grant an access token. Check the approved app credentials."
        )
    return token


def _reddit_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError
    return datetime.fromtimestamp(value, UTC).isoformat()


def _reddit(source: CollectionSource, client: _APIClient) -> CollectedBatch:
    subreddit = normalize_locator("reddit", source.locator)
    agent = os.environ.get("REDDIT_USER_AGENT", _DEFAULT_AGENT).strip()
    if not agent or len(agent) > 256 or any(ord(char) < 32 or ord(char) > 126 for char in agent):
        raise CollectionFailure("Reddit needs a valid descriptive server-side user agent.")
    token = _reddit_token(client, agent)
    endpoint = f"https://oauth.reddit.com/r/{subreddit}/new"
    payload = client.request(
        "GET",
        endpoint,
        headers={"Authorization": "Bearer " + token, "User-Agent": agent},
        params={"limit": source.item_limit, "raw_json": 1},
    )
    listing = payload.get("data")
    if (
        payload.get("kind") != "Listing"
        or not isinstance(listing, dict)
        or not isinstance(listing.get("children"), list)
    ):
        raise CollectionFailure("Reddit returned invalid community post data.")
    acquired = datetime.now(UTC).isoformat()
    items, warnings, skipped = [], [], 0
    children = listing["children"]
    for child in children[: source.item_limit]:
        try:
            if (
                not isinstance(child, dict)
                or child.get("kind") != "t3"
                or not isinstance(child.get("data"), dict)
            ):
                raise ValueError
            post = child["data"]
            native = post.get("id")
            if not isinstance(native, str) or not _REDDIT_ID.fullmatch(native):
                raise ValueError
            if str(post.get("subreddit", subreddit)).lower() != subreddit:
                raise ValueError
            if post.get("removed_by_category") or post.get("selftext") in {
                "[removed]",
                "[deleted]",
            }:
                raise ValueError
            title, body = post.get("title"), post.get("selftext", "")
            if not isinstance(title, str) or not title.strip() or not isinstance(body, str):
                raise ValueError
            permalink = post.get("permalink")
            if not isinstance(permalink, str):
                raise ValueError
            parsed = urlsplit(permalink)
            if (
                parsed.scheme
                or parsed.netloc
                or not permalink.startswith(f"/r/{subreddit}/comments/")
            ):
                # Reddit may return a mixed-case subreddit path.
                if (
                    parsed.scheme
                    or parsed.netloc
                    or not permalink.lower().startswith(f"/r/{subreddit}/comments/")
                ):
                    raise ValueError
            public_url = "https://www.reddit.com" + permalink
            author = post.get("author")
            if author in {None, "[deleted]"}:
                author = None
            refs = post.get("crosspost_parent_list", [])
            repost = None
            for original in refs[:1] if isinstance(refs, list) else []:
                if not isinstance(original, dict):
                    continue
                channel, original_id = original.get("subreddit"), original.get("id")
                if (
                    isinstance(channel, str)
                    and isinstance(original_id, str)
                    and _REDDIT_ID.fullmatch(original_id)
                ):
                    repost = SourceIdentity(
                        platform="reddit", channel_id=channel.lower(), native_id=original_id
                    )
            items.append(
                SourceItem(
                    platform="reddit",
                    channel_id=subreddit,
                    channel_name=source.label,
                    native_id=native,
                    text=title + ("\n\n" + body if body else ""),
                    title=title,
                    source_url=public_url,
                    author_id=author,
                    published_at=_reddit_timestamp(post.get("created_utc")),
                    acquired_at=acquired,
                    shared_urls=_urls([post.get("url_overridden_by_dest"), post.get("url")]),
                    repost_of=repost,
                    provenance={
                        "method": "reddit_api",
                        "content_scope": "post_text",
                        "retrieved_url": endpoint,
                    },
                )
            )
        except (ValueError, TypeError, ValidationError, OverflowError, OSError):
            skipped += 1
    if skipped:
        warnings.append(f"Reddit omitted {skipped} unavailable or invalid source records.")
    if listing.get("after") or len(children) > source.item_limit:
        warnings.append(
            "Reddit collection covers the latest bounded post window; older posts may be omitted."
        )
    return CollectedBatch(
        items=tuple(items),
        warnings=tuple(warnings),
        cursor=("t3_" + items[0].native_id) if items else source.cursor,
    )


def collect_api(
    source: CollectionSource,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
) -> CollectedBatch:
    """Collect one bounded read-only API window, failing clearly when access is missing."""
    if source.platform not in {"x", "reddit"}:
        raise CollectionFailure("Choose X or Reddit for authenticated collection.")
    status = configuration_status(source.platform)
    if not status["ready"]:
        raise CollectionFailure(status["message"])
    with _APIClient(source.platform, transport=transport, resolver=resolver) as client:
        return _x(source, client) if source.platform == "x" else _reddit(source, client)
