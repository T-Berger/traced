"""OAuth 2.1 sign-in with PKCE for the Maltego Data MCP.

Discovery follows the MCP authorization flow: the protected-resource metadata
(RFC 9728) names the authorization server, whose metadata (RFC 8414 or OpenID
Connect discovery) supplies the endpoints. The person signs in on the identity
provider's own page in their browser, so Traced never handles a password. The
browser returns to a single-use listener bound to 127.0.0.1 (RFC 8252); the
response is checked for state and issuer (RFC 9207), and the resource indicator
(RFC 8707) binds the tokens to the Data MCP endpoint.

Authorization codes, verifiers, and tokens never appear in messages, logs, or
representations. Every failure raised here is safe to show to the person.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from ...source_contracts import safe_source_url
from ..models import CollectionFailure
from .settings import USER_AGENT, MaltegoSettings
from .tokens import OAuthTokens, TokenStore, TokenStoreError

CALLBACK_PATH = "/callback"
DEFAULT_SCOPES = ("openid", "profile", "offline_access")
SIGN_IN_HINT = "Run ./traced maltego login and sign in with your own Maltego account."

_METADATA_LIMIT = 256 * 1024
_TOKEN_RESPONSE_LIMIT = 256 * 1024
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_SCOPE = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]{1,100}")
_ERROR_CODE = re.compile(r"[A-Za-z0-9_.-]{1,80}")


class OAuthFailure(CollectionFailure):
    """A user-safe sign-in problem that never includes a token or code."""


class NotSignedIn(OAuthFailure):
    """No usable sign-in exists for this Data MCP endpoint and client."""


@dataclass(frozen=True)
class ResourceMetadata:
    resource: str
    authorization_server: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class AuthorizationServer:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    revocation_endpoint: str | None
    iss_parameter_supported: bool


@dataclass(frozen=True, repr=False)
class AuthorizationRequest:
    url: str
    state: str
    verifier: str
    redirect_uri: str

    def __repr__(self) -> str:
        return f"AuthorizationRequest(redirect_uri={self.redirect_uri!r})"


@dataclass(frozen=True, repr=False)
class CallbackResult:
    code: str | None
    issuer: str | None
    error: str | None
    error_description: str | None

    def __repr__(self) -> str:
        return f"CallbackResult(error={self.error!r}, has_code={self.code is not None})"


@dataclass(frozen=True)
class SignInResult:
    issuer: str
    access_expires_at: float | None
    refreshable: bool
    token_path: Path


@dataclass(frozen=True)
class SignOutResult:
    removed: bool
    revoked: bool | None


@dataclass(frozen=True)
class SignInStatus:
    state: Literal["signed_out", "signed_in", "expired", "unreadable"]
    message: str
    access_expires_at: float | None = None
    refreshable: bool = False

    @property
    def ready(self) -> bool:
        return self.state == "signed_in"


def _client(transport: httpx.BaseTransport | None) -> httpx.Client:
    return httpx.Client(
        transport=transport,
        timeout=_HTTP_TIMEOUT,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
    )


def _safe_text(value: object, limit: int = 200) -> str:
    if not isinstance(value, str):
        return "unknown reason"
    cleaned = "".join(char for char in value if char.isprintable()).strip()
    return cleaned[:limit] or "unknown reason"


def _https_endpoint(value: object, label: str) -> str:
    failure = OAuthFailure(f"The sign-in metadata has no valid HTTPS {label}.")
    if not isinstance(value, str):
        raise failure
    try:
        safe_source_url(value)
    except ValueError:
        raise failure from None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.fragment or "#" in value:
        raise failure
    return value


def _read_limited(response: httpx.Response, limit: int) -> bytes:
    chunks, size = [], 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > limit:
            raise OAuthFailure("The sign-in server returned an unexpectedly large response.")
        chunks.append(chunk)
    return b"".join(chunks)


def _json_object(body: bytes, message: str) -> dict:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise OAuthFailure(message) from None
    if not isinstance(data, dict):
        raise OAuthFailure(message)
    return data


def _get_metadata(client: httpx.Client, url: str) -> dict | None:
    """Return a JSON document, or None when this discovery location does not exist."""
    try:
        with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
            if response.status_code in {404, 405}:
                return None
            if response.is_redirect:
                raise OAuthFailure("Sign-in discovery was redirected; Traced does not follow it.")
            if response.status_code != 200:
                raise OAuthFailure(f"Sign-in discovery failed with HTTP {response.status_code}.")
            body = _read_limited(response, _METADATA_LIMIT)
    except httpx.TimeoutException:
        raise OAuthFailure("Sign-in discovery timed out. Try again later.") from None
    except httpx.HTTPError:
        raise OAuthFailure("Sign-in discovery could not reach the server.") from None
    return _json_object(body, "Sign-in discovery returned unreadable metadata.")


def discover_resource(settings: MaltegoSettings, client: httpx.Client) -> ResourceMetadata:
    path = urlsplit(settings.server_url).path.rstrip("/")
    candidates = [settings.origin + "/.well-known/oauth-protected-resource" + path]
    if path:
        candidates.append(settings.origin + "/.well-known/oauth-protected-resource")
    for url in candidates:
        data = _get_metadata(client, url)
        if data is None:
            continue
        resource = data.get("resource")
        if not isinstance(resource, str) or resource.rstrip("/") != settings.server_url:
            raise OAuthFailure(
                "The Data MCP metadata names a different resource. Check TRACED_MALTEGO_MCP_URL."
            )
        servers = data.get("authorization_servers")
        if not isinstance(servers, list) or not servers:
            raise OAuthFailure("The Data MCP metadata names no identity provider.")
        issuer = _https_endpoint(servers[0], "identity provider")
        scopes = data.get("scopes_supported")
        selected = tuple(
            scope
            for scope in (scopes if isinstance(scopes, list) else [])
            if isinstance(scope, str) and _SCOPE.fullmatch(scope)
        )
        return ResourceMetadata(settings.server_url, issuer, selected)
    raise OAuthFailure("The Data MCP did not publish sign-in metadata.")


def discover_authorization_server(issuer: str, client: httpx.Client) -> AuthorizationServer:
    parsed = urlsplit(issuer)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    candidates = [
        origin + "/.well-known/oauth-authorization-server" + path,
        origin + "/.well-known/openid-configuration" + path,
    ]
    if path:
        candidates.append(issuer.rstrip("/") + "/.well-known/openid-configuration")
    for url in candidates:
        data = _get_metadata(client, url)
        if data is None:
            continue
        if data.get("issuer") != issuer:
            raise OAuthFailure("The identity provider metadata names a different issuer.")
        methods = data.get("code_challenge_methods_supported")
        if not isinstance(methods, list) or "S256" not in methods:
            raise OAuthFailure(
                "The identity provider does not offer PKCE with S256, so Traced will not sign in."
            )
        revocation = data.get("revocation_endpoint")
        return AuthorizationServer(
            issuer=issuer,
            authorization_endpoint=_https_endpoint(
                data.get("authorization_endpoint"), "authorization endpoint"
            ),
            token_endpoint=_https_endpoint(data.get("token_endpoint"), "token endpoint"),
            revocation_endpoint=(
                _https_endpoint(revocation, "revocation endpoint") if revocation else None
            ),
            iss_parameter_supported=data.get("authorization_response_iss_parameter_supported")
            is True,
        )
    raise OAuthFailure("The identity provider did not publish sign-in metadata.")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def new_state() -> str:
    return _b64url(secrets.token_bytes(24))


def new_authorization_request(
    server: AuthorizationServer,
    resource: ResourceMetadata,
    client_id: str,
    redirect_uri: str,
    *,
    state: str | None = None,
) -> AuthorizationRequest:
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = state or new_state()
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(resource.scopes or DEFAULT_SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": resource.resource,
    }
    separator = "&" if urlsplit(server.authorization_endpoint).query else "?"
    url = server.authorization_endpoint + separator + urlencode(params)
    return AuthorizationRequest(url, state, verifier, redirect_uri)


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;
line-height:1.5;color:#1d2433}}</style></head><body><h1>{title}</h1><p>{message}</p></body></html>
"""
_SUCCESS = _PAGE.format(
    title="Signed in to the Maltego Data MCP",
    message="Traced received the sign-in. You can close this tab and return to the terminal.",
)
_REFUSED = _PAGE.format(
    title="Sign-in not completed",
    message="Traced did not receive a usable sign-in. Return to the terminal for details.",
)
_FOREIGN = _PAGE.format(
    title="Sign-in response not accepted",
    message="This response does not belong to the current Traced sign-in and was ignored.",
)


class _CallbackServer(HTTPServer):
    def __init__(self, port: int, path: str, expected_state: str):
        super().__init__(("127.0.0.1", port), _CallbackHandler)
        self.callback_path = path
        self.expected_state = expected_state
        self.result: CallbackResult | None = None
        self.received = threading.Event()


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackServer

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlsplit(self.path)
        if parsed.path != self.server.callback_path:
            self._reply(404, _FOREIGN)
            return
        try:
            params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
        except ValueError:
            self._reply(400, _FOREIGN)
            return

        def single(name: str) -> str | None:
            values = params.get(name)
            return values[0] if values and len(values) == 1 and len(values[0]) <= 4096 else None

        state = single("state")
        expected = self.server.expected_state.encode()
        if state is None or not hmac.compare_digest(state.encode(), expected):
            self._reply(400, _FOREIGN)
            return
        if self.server.received.is_set():
            self._reply(409, _FOREIGN)
            return
        result = CallbackResult(
            code=single("code") or None,
            issuer=single("iss"),
            error=single("error"),
            error_description=single("error_description"),
        )
        self.server.result = result
        self.server.received.set()
        self._reply(200, _SUCCESS if result.code and not result.error else _REFUSED)

    def _reply(self, status: int, page: str) -> None:
        body = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Never log request lines: they carry the authorization code."""


class LoopbackReceiver:
    """Single-use authorization-response listener bound to 127.0.0.1 only."""

    def __init__(self, port: int, expected_state: str, path: str = CALLBACK_PATH):
        try:
            self._server = _CallbackServer(port, path, expected_state)
        except OSError:
            raise OAuthFailure(
                f"Port {port} on 127.0.0.1 is busy. Close the other sign-in, or set "
                "TRACED_MALTEGO_CALLBACK_PORT to another port (0 picks a free one)."
            ) from None
        self._path = path
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True
        )

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}{self._path}"

    def start(self) -> LoopbackReceiver:
        self._thread.start()
        return self

    def wait(self, timeout: float) -> CallbackResult:
        if not self._server.received.wait(timeout) or self._server.result is None:
            raise OAuthFailure(
                f"The sign-in was not completed within {round(timeout / 60, 1)} minutes. "
                + SIGN_IN_HINT
            )
        return self._server.result

    def close(self) -> None:
        if self._thread.is_alive():
            self._server.shutdown()
            self._thread.join(timeout=2)
        self._server.server_close()

    def __enter__(self) -> LoopbackReceiver:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _visible_token(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 65_536
        or any(not 33 <= ord(char) <= 126 for char in value)
    ):
        raise OAuthFailure(f"The identity provider returned an unusable {label}.")
    return value


def _seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return float(min(value, 10 * 365 * 24 * 3600))


def _token_request(client: httpx.Client, endpoint: str, form: dict[str, str]) -> dict:
    _https_endpoint(endpoint, "token endpoint")
    try:
        with client.stream(
            "POST", endpoint, data=form, headers={"Accept": "application/json"}
        ) as response:
            body = _read_limited(response, _TOKEN_RESPONSE_LIMIT)
            status = response.status_code
    except httpx.TimeoutException:
        raise OAuthFailure("The identity provider did not answer in time. Try again.") from None
    except httpx.HTTPError:
        raise OAuthFailure("The identity provider could not be reached.") from None
    if status == 200:
        return _json_object(body, "The identity provider returned an unreadable token response.")
    try:
        error = _json_object(body, "").get("error")
    except OAuthFailure:
        error = None
    if error == "invalid_grant":
        raise NotSignedIn("Your Maltego sign-in has expired or was revoked. " + SIGN_IN_HINT)
    code = error if isinstance(error, str) and _ERROR_CODE.fullmatch(error) else f"HTTP {status}"
    raise OAuthFailure(f"The identity provider refused the token request ({code}).")


def _tokens_from_response(
    data: dict,
    *,
    issuer: str,
    client_id: str,
    resource: str,
    token_endpoint: str,
    revocation_endpoint: str | None,
    now: float,
) -> OAuthTokens:
    token_type = data.get("token_type")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise OAuthFailure("The identity provider did not return a bearer token.")
    access_token = _visible_token(data.get("access_token"), "access token")
    refresh = data.get("refresh_token")
    refresh_token = _visible_token(refresh, "refresh token") if refresh is not None else None
    expires_in = _seconds(data.get("expires_in"))
    # Keycloak reports 0 for offline refresh tokens without a fixed expiry.
    refresh_expires_in = _seconds(data.get("refresh_expires_in"))
    scope = data.get("scope")
    return OAuthTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=now + expires_in if expires_in else None,
        refresh_expires_at=now + refresh_expires_in if refresh_expires_in else None,
        scope=scope if isinstance(scope, str) and len(scope) <= 2000 else "",
        issuer=issuer,
        client_id=client_id,
        resource=resource,
        token_endpoint=token_endpoint,
        revocation_endpoint=revocation_endpoint,
        obtained_at=now,
    )


def sign_in(
    settings: MaltegoSettings,
    *,
    store: TokenStore | None = None,
    open_browser: Callable[[str], object] = webbrowser.open,
    notify: Callable[[str], None] = print,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 300.0,
    clock: Callable[[], float] = time.time,
) -> SignInResult:
    """Run the browser sign-in and save the resulting tokens for this person only."""
    store = store or TokenStore(settings.token_path)
    with _client(transport) as client:
        resource = discover_resource(settings, client)
        server = discover_authorization_server(resource.authorization_server, client)
        state = new_state()
        with LoopbackReceiver(settings.callback_port, state).start() as receiver:
            request = new_authorization_request(
                server, resource, settings.client_id, receiver.redirect_uri, state=state
            )
            notify(
                "Sign in with your own Maltego account in the browser window that opens.\n"
                "If no window opens, visit this address on this computer:\n" + request.url
            )
            try:
                open_browser(request.url)
            except Exception:  # noqa: BLE001 - the printed address remains available.
                notify("The browser could not be opened automatically; use the address above.")
            callback = receiver.wait(timeout)
        if callback.error is not None:
            reason = _safe_text(callback.error_description or callback.error)
            raise OAuthFailure("The Maltego sign-in was not completed: " + reason)
        if callback.issuer is None and server.iss_parameter_supported:
            raise OAuthFailure("The sign-in response did not name its issuer, so it was rejected.")
        if callback.issuer is not None and callback.issuer != server.issuer:
            raise OAuthFailure(
                "The sign-in response came from a different issuer and was rejected."
            )
        if not callback.code:
            raise OAuthFailure("The sign-in response contained no authorization code.")
        data = _token_request(
            client,
            server.token_endpoint,
            {
                "grant_type": "authorization_code",
                "code": callback.code,
                "redirect_uri": request.redirect_uri,
                "client_id": settings.client_id,
                "code_verifier": request.verifier,
                "resource": resource.resource,
            },
        )
    tokens = _tokens_from_response(
        data,
        issuer=server.issuer,
        client_id=settings.client_id,
        resource=resource.resource,
        token_endpoint=server.token_endpoint,
        revocation_endpoint=server.revocation_endpoint,
        now=clock(),
    )
    with store.lock():
        store.save(tokens)
    return SignInResult(
        server.issuer, tokens.expires_at, tokens.refresh_token is not None, store.path
    )


def refresh_tokens(
    tokens: OAuthTokens,
    *,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], float] = time.time,
) -> OAuthTokens:
    if not tokens.refresh_usable(clock()):
        raise NotSignedIn("Your Maltego sign-in has expired. " + SIGN_IN_HINT)
    with _client(transport) as client:
        data = _token_request(
            client,
            tokens.token_endpoint,
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens.refresh_token or "",
                "client_id": tokens.client_id,
                "resource": tokens.resource,
            },
        )
    refreshed = _tokens_from_response(
        data,
        issuer=tokens.issuer,
        client_id=tokens.client_id,
        resource=tokens.resource,
        token_endpoint=tokens.token_endpoint,
        revocation_endpoint=tokens.revocation_endpoint,
        now=clock(),
    )
    return tokens.with_refresh(refreshed)


class TokenProvider:
    """Return a usable access token, refreshing under the store lock when needed."""

    def __init__(
        self,
        settings: MaltegoSettings,
        store: TokenStore | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.store = store or TokenStore(settings.token_path)
        self.transport = transport
        self.clock = clock

    def _load(self) -> OAuthTokens:
        try:
            tokens = self.store.load()
        except TokenStoreError as exc:
            raise NotSignedIn(f"{exc} {SIGN_IN_HINT}") from None
        if tokens is None or not tokens.matches(
            resource=self.settings.server_url, client_id=self.settings.client_id
        ):
            raise NotSignedIn("Traced is not signed in to the Maltego Data MCP. " + SIGN_IN_HINT)
        return tokens

    def access_token(self, *, rejected: str | None = None) -> str:
        tokens = self._load()
        if tokens.access_token != rejected and tokens.access_usable(self.clock()):
            return tokens.access_token
        with self.store.lock():
            # Another process may have refreshed while this one waited for the lock.
            tokens = self._load()
            if tokens.access_token != rejected and tokens.access_usable(self.clock()):
                return tokens.access_token
            refreshed = refresh_tokens(tokens, transport=self.transport, clock=self.clock)
            self.store.save(refreshed)
            return refreshed.access_token


def sign_out(
    settings: MaltegoSettings,
    *,
    store: TokenStore | None = None,
    transport: httpx.BaseTransport | None = None,
) -> SignOutResult:
    """Revoke the refresh token when possible, then delete the local sign-in."""
    store = store or TokenStore(settings.token_path)
    with store.lock():
        try:
            tokens = store.load()
        except TokenStoreError:
            tokens = None
        revoked = None
        if tokens is not None and tokens.revocation_endpoint:
            token, hint = (
                (tokens.refresh_token, "refresh_token")
                if tokens.refresh_token
                else (tokens.access_token, "access_token")
            )
            try:
                endpoint = _https_endpoint(tokens.revocation_endpoint, "revocation endpoint")
                with _client(transport) as client:
                    response = client.post(
                        endpoint,
                        data={
                            "token": token,
                            "token_type_hint": hint,
                            "client_id": tokens.client_id,
                        },
                    )
                revoked = response.status_code == 200
            except (OAuthFailure, httpx.HTTPError):
                revoked = False
        removed = store.clear()
    return SignOutResult(removed, revoked)


def sign_in_status(
    settings: MaltegoSettings,
    store: TokenStore | None = None,
    *,
    clock: Callable[[], float] = time.time,
) -> SignInStatus:
    """Describe the saved sign-in without a network request or any secret."""
    store = store or TokenStore(settings.token_path)
    try:
        tokens = store.load()
    except TokenStoreError:
        return SignInStatus(
            "unreadable", "The saved Maltego sign-in is unreadable. " + SIGN_IN_HINT
        )
    if tokens is None or not tokens.matches(
        resource=settings.server_url, client_id=settings.client_id
    ):
        return SignInStatus("signed_out", "Not signed in to the Maltego Data MCP. " + SIGN_IN_HINT)
    now = clock()
    if tokens.access_usable(now) or tokens.refresh_usable(now):
        return SignInStatus(
            "signed_in",
            "Signed in to the Maltego Data MCP; access renews automatically."
            if tokens.refresh_usable(now)
            else "Signed in to the Maltego Data MCP until the current access expires.",
            tokens.expires_at,
            tokens.refresh_usable(now),
        )
    return SignInStatus(
        "expired", "The Maltego sign-in has expired. " + SIGN_IN_HINT, tokens.expires_at
    )
