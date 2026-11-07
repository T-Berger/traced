"""Sign-in uses discovery, PKCE, and a loopback listener without exposing secrets."""

import re
import threading
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from evidence_explorer.collection.maltego import oauth
from evidence_explorer.collection.maltego.oauth import (
    LoopbackReceiver,
    NotSignedIn,
    OAuthFailure,
    TokenProvider,
    sign_in,
    sign_out,
)
from evidence_explorer.collection.maltego.tokens import TokenStore


def browser(fake, responses, rewrite=None):
    """Follow the sign-in address the way a person's browser would after signing in."""
    opened = []

    def open_browser(url):
        opened.append(url)
        redirect = fake.authorize(url)
        if rewrite is not None:
            redirect = rewrite(redirect)

        def follow():
            responses.append(httpx.get(redirect, timeout=5))

        threading.Thread(target=follow, daemon=True).start()
        return True

    return open_browser, opened


def quiet(_message):
    return None


def test_sign_in_discovers_endpoints_uses_pkce_and_saves_tokens_for_one_user(fake, settings):
    responses, messages = [], []
    open_browser, opened = browser(fake, responses)
    result = sign_in(
        settings,
        open_browser=open_browser,
        notify=messages.append,
        transport=fake.transport,
        timeout=10,
    )
    query = parse_qs(urlsplit(opened[0]).query)
    assert opened[0].startswith(fake.ISSUER + "/protocol/openid-connect/auth?")
    assert query["code_challenge_method"] == ["S256"] and len(query["code_challenge"][0]) == 43
    assert query["client_id"] == [fake.CLIENT_ID] and query["resource"] == [fake.RESOURCE]
    assert query["scope"] == ["openid profile offline_access"]
    redirect = query["redirect_uri"][0]
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/callback", redirect)
    assert fake.requests[0].url.path == "/.well-known/oauth-protected-resource/mcp"
    assert responses[0].status_code == 200 and "Signed in" in responses[0].text
    assert result.refreshable and result.token_path == settings.token_path
    saved = settings.token_path.read_text()
    assert "fixture-id-token" not in saved and "code-1" not in saved
    assert not any("access-1" in message or "refresh-1" in message for message in messages)
    assert TokenStore(settings.token_path).load().access_token == "access-1"


@pytest.mark.parametrize(
    "rewrite, message",
    [
        (lambda url: url.replace("identity.example%2Frealms%2Ftest", "evil.example"), "issuer"),
        (lambda url: re.sub(r"&iss=[^&]+", "", url), "did not name its issuer"),
        (
            lambda url: url.replace("code=code-1", "error=access_denied&error_description=Closed"),
            "not completed: Closed",
        ),
    ],
)
def test_foreign_issuers_and_provider_errors_are_rejected(fake, settings, rewrite, message):
    responses = []
    open_browser, _ = browser(fake, responses, rewrite)
    with pytest.raises(OAuthFailure, match=message):
        sign_in(
            settings,
            open_browser=open_browser,
            notify=quiet,
            transport=fake.transport,
            timeout=10,
        )
    assert not settings.token_path.exists()


def test_loopback_listener_accepts_only_the_matching_response():
    with LoopbackReceiver(0, "expected-state").start() as receiver:
        base = receiver.redirect_uri
        foreign = httpx.get(base + "?code=stolen&state=other", timeout=5)
        elsewhere = httpx.get(base.removesuffix("/callback") + "/favicon.ico", timeout=5)
        genuine = httpx.get(base + "?code=abc&state=expected-state", timeout=5)
        repeated = httpx.get(base + "?code=xyz&state=expected-state", timeout=5)
        result = receiver.wait(5)
    statuses = (foreign.status_code, elsewhere.status_code, genuine.status_code)
    assert statuses + (repeated.status_code,) == (400, 404, 200, 409)
    assert result.code == "abc" and "abc" not in repr(result)
    assert genuine.headers["cache-control"] == "no-store"
    assert "stolen" not in foreign.text and "abc" not in genuine.text


def test_listener_binds_only_loopback_times_out_and_reports_busy_ports():
    with LoopbackReceiver(0, "state").start() as receiver:
        assert receiver._server.server_address[0] == "127.0.0.1"
        port = receiver._server.server_address[1]
        with pytest.raises(OAuthFailure, match="busy"):
            LoopbackReceiver(port, "other")
        with pytest.raises(OAuthFailure, match="not completed"):
            receiver.wait(0.2)


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda f: f.resource_metadata.update(resource="https://other.example/mcp"), "resource"),
        (lambda f: f.resource_metadata.update(authorization_servers=[]), "no identity provider"),
        (
            lambda f: f.server_metadata.update(code_challenge_methods_supported=["plain"]),
            "S256",
        ),
        (lambda f: f.server_metadata.update(issuer=f.ISSUER + "-other"), "different issuer"),
        (
            lambda f: f.server_metadata.update(token_endpoint="http://identity.example/token"),
            "HTTPS token endpoint",
        ),
    ],
)
def test_inconsistent_metadata_stops_sign_in_before_a_browser_opens(
    fake, settings, change, message
):
    change(fake)
    opened = []
    with pytest.raises(OAuthFailure, match=message):
        sign_in(
            settings, open_browser=opened.append, notify=quiet, transport=fake.transport, timeout=1
        )
    assert opened == []


def test_discovery_never_follows_redirects(settings):
    def redirect(_request):
        return httpx.Response(302, headers={"location": "https://elsewhere.example/"})

    with pytest.raises(OAuthFailure, match="redirected"):
        sign_in(
            settings,
            open_browser=lambda _url: True,
            notify=quiet,
            transport=httpx.MockTransport(redirect),
            timeout=1,
        )


def test_resource_metadata_falls_back_to_the_origin_location(fake, settings):
    def handle(request):
        if request.url.path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(404)
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx.Response(200, json=fake.resource_metadata)
        return fake.handle(request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        metadata = oauth.discover_resource(settings, client)
        server = oauth.discover_authorization_server(metadata.authorization_server, client)
    assert metadata.authorization_server == fake.ISSUER
    assert server.token_endpoint.endswith("/protocol/openid-connect/token")
    assert server.iss_parameter_supported


def test_expired_access_refreshes_once_and_keeps_the_rotated_refresh_token(
    fake, settings, make_tokens
):
    store = TokenStore(settings.token_path)
    store.save(make_tokens(expires_in=-5))
    provider = TokenProvider(settings, store, transport=fake.transport)
    assert provider.access_token() == "access-2"
    assert provider.access_token() == "access-2"
    assert store.load().refresh_token == "refresh-2"
    token_requests = [r for r in fake.requests if r.url.path.endswith("/token")]
    assert len(token_requests) == 1
    form = parse_qs(token_requests[0].content.decode())
    assert form["grant_type"] == ["refresh_token"] and form["resource"] == [fake.RESOURCE]


def test_a_rejected_token_is_refreshed_and_an_unrotated_refresh_token_is_kept(
    fake, settings, make_tokens
):
    fake.refreshable = {"refresh-1": ("access-2", None)}
    store = TokenStore(settings.token_path)
    store.save(make_tokens())
    provider = TokenProvider(settings, store, transport=fake.transport)
    assert provider.access_token() == "access-1"
    assert provider.access_token(rejected="access-1") == "access-2"
    assert store.load().refresh_token == "refresh-1"


def test_revoked_refresh_asks_the_person_to_sign_in_again(fake, settings, make_tokens):
    fake.refreshable = {}
    store = TokenStore(settings.token_path)
    store.save(make_tokens(expires_in=-5))
    with pytest.raises(NotSignedIn, match="./traced maltego login"):
        TokenProvider(settings, store, transport=fake.transport).access_token()


def test_missing_or_foreign_sign_in_is_never_used(settings, make_tokens):
    store = TokenStore(settings.token_path)
    with pytest.raises(NotSignedIn):
        TokenProvider(settings, store).access_token()
    store.save(replace(make_tokens(), resource="https://other.example/mcp"))
    with pytest.raises(NotSignedIn):
        TokenProvider(settings, store).access_token()


def test_sign_out_revokes_the_refresh_token_and_deletes_the_file(fake, settings, signed_in):
    result = sign_out(settings, store=signed_in, transport=fake.transport)
    assert result.removed and result.revoked is True
    assert fake.revoked == [
        {"token": "refresh-1", "token_type_hint": "refresh_token", "client_id": fake.CLIENT_ID}
    ]
    assert not settings.token_path.exists()
    assert sign_out(settings, store=signed_in, transport=fake.transport).removed is False
