"""Fixture identity provider and Data MCP endpoint; tests never use the network.

The fixture follows the shapes observed from the real service on 7 November
2026: RFC 9728 resource metadata at a path-aware location, a Keycloak realm
publishing OpenID discovery only under the issuer path, PKCE S256, loopback
redirects, and a Streamable HTTP MCP endpoint that answers 401 without a token.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from evidence_explorer.collection.maltego.settings import MaltegoSettings
from evidence_explorer.collection.maltego.tokens import OAuthTokens, TokenStore

RESOURCE = "https://datamcp.example/mcp"
ISSUER = "https://identity.example/realms/test"
CLIENT_ID = "traced-test-client"
AUTHORIZE_URL = ISSUER + "/protocol/openid-connect/auth"
TOKEN_URL = ISSUER + "/protocol/openid-connect/token"
REVOKE_URL = ISSUER + "/protocol/openid-connect/revoke"


class FakeMaltego:
    """Routes identity, metadata, and MCP requests by host and path."""

    RESOURCE = RESOURCE
    ISSUER = ISSUER
    CLIENT_ID = CLIENT_ID

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.valid_access = {"access-1"}
        self.refreshable = {"refresh-1": ("access-2", "refresh-2")}
        self.codes: dict[str, dict] = {}
        self.revoked: list[dict] = []
        self.replies: list[dict] = []
        self.active_sessions: set[str] = set()
        self.reject_all = False
        self.initialized = 0
        self.protocol = "2025-06-18"
        self.tool_pages = [
            [self.tool("maltego_search"), self.tool("maltego_describe")],
            [self.tool("maltego_run")],
        ]
        self.on_call = self.default_call
        self.on_get = None
        self.resource_metadata = {
            "resource": RESOURCE,
            "authorization_servers": [ISSUER],
            "scopes_supported": ["openid", "profile", "offline_access"],
        }
        self.server_metadata = {
            "issuer": ISSUER,
            "authorization_endpoint": AUTHORIZE_URL,
            "token_endpoint": TOKEN_URL,
            "revocation_endpoint": REVOKE_URL,
            "code_challenge_methods_supported": ["plain", "S256"],
            "authorization_response_iss_parameter_supported": True,
        }

    # Message helpers shared by tests.
    @staticmethod
    def tool(name: str) -> dict:
        return {
            "name": name,
            "description": f"Fixture tool {name}.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "What to look up"}},
                "required": ["query"],
            },
        }

    @staticmethod
    def text_result(text: str, *, error: bool = False) -> dict:
        result: dict = {"content": [{"type": "text", "text": text}]}
        if error:
            result["isError"] = True
        return result

    @staticmethod
    def rpc(request_id, result=None, error=None) -> dict:
        message = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result
        return message

    @staticmethod
    def sse(*events, status: int = 200) -> httpx.Response:
        """Each event is (event_id or None, message dict, or raw data string)."""
        lines = []
        for event_id, message in events:
            if event_id is not None:
                lines.append(f"id: {event_id}")
            data = message if isinstance(message, str) else json.dumps(message)
            lines.extend(f"data: {part}" for part in data.split("\n"))
            lines.append("")
        body = ("\r\n".join(lines) + "\r\n").encode()
        return httpx.Response(status, headers={"content-type": "text/event-stream"}, content=body)

    def default_call(self, request_id, name, arguments) -> httpx.Response:
        text = f"{name} result for {json.dumps(arguments, sort_keys=True)}"
        return httpx.Response(200, json=self.rpc(request_id, self.text_result(text)))

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def mcp_requests(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path == "/mcp"]

    def authorize(self, url: str) -> str:
        """Act as the identity provider after a successful sign-in; returns the redirect."""
        query = parse_qs(urlsplit(url).query)
        code = f"code-{len(self.codes) + 1}"
        self.codes[code] = {
            "challenge": query["code_challenge"][0],
            "redirect_uri": query["redirect_uri"][0],
        }
        params = {"code": code, "state": query["state"][0], "iss": ISSUER}
        return query["redirect_uri"][0] + "?" + urlencode(params)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "datamcp.example" and path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(200, json=self.resource_metadata)
        if host == "identity.example":
            if path == "/realms/test/.well-known/openid-configuration":
                return httpx.Response(200, json=self.server_metadata)
            if path == "/realms/test/protocol/openid-connect/token":
                return self.token(request)
            if path == "/realms/test/protocol/openid-connect/revoke":
                self.revoked.append(
                    {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
                )
                return httpx.Response(200)
            return httpx.Response(404)
        if host == "datamcp.example" and path == "/mcp":
            return self.mcp(request)
        return httpx.Response(404)

    def token(self, request: httpx.Request) -> httpx.Response:
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        if form.get("client_id") != CLIENT_ID:
            return httpx.Response(401, json={"error": "invalid_client"})
        if form.get("grant_type") == "authorization_code":
            grant = self.codes.pop(form.get("code", ""), None)
            verifier = form.get("code_verifier", "").encode()
            digest = hashlib.sha256(verifier).digest()
            expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            if (
                grant is None
                or grant["challenge"] != expected
                or grant["redirect_uri"] != form.get("redirect_uri")
                or form.get("resource") != RESOURCE
            ):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "access_token": "access-1",
                    "token_type": "Bearer",
                    "expires_in": 300,
                    "refresh_token": "refresh-1",
                    "refresh_expires_in": 0,
                    "scope": "openid profile offline_access",
                    "id_token": "fixture-id-token-with-personal-claims",
                },
            )
        if form.get("grant_type") == "refresh_token":
            issued = self.refreshable.pop(form.get("refresh_token", ""), None)
            if issued is None:
                return httpx.Response(
                    400, json={"error": "invalid_grant", "error_description": "Token is not active"}
                )
            access, refresh = issued
            self.valid_access.add(access)
            body = {"access_token": access, "token_type": "bearer", "expires_in": 300}
            if refresh:
                body["refresh_token"] = refresh
            return httpx.Response(200, json=body)
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def mcp(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if self.reject_all or not auth.startswith("Bearer ") or auth[7:] not in self.valid_access:
            return httpx.Response(401, json={"detail": "Invalid authorization credentials"})
        session = request.headers.get("mcp-session-id")
        if request.method == "DELETE":
            self.active_sessions.discard(session)
            return httpx.Response(200)
        if request.method == "GET":
            return self.on_get(request) if self.on_get else httpx.Response(405)
        message = json.loads(request.content)
        method = message.get("method")
        if method == "initialize":
            self.initialized += 1
            created = f"session-{self.initialized}"
            self.active_sessions.add(created)
            result = {
                "protocolVersion": self.protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "Fixture Data MCP", "version": "9.9"},
                "instructions": "Search first, then describe a capability before running it.",
            }
            return httpx.Response(
                200, headers={"mcp-session-id": created}, json=self.rpc(message["id"], result)
            )
        if session not in self.active_sessions:
            return httpx.Response(404, json={"error": {"code": -32001, "message": "No session"}})
        if request.headers.get("mcp-protocol-version") != self.protocol:
            return httpx.Response(
                400, json={"error": {"code": -32600, "message": "Missing protocol version"}}
            )
        if method is None:
            self.replies.append(message)
            return httpx.Response(202)
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            cursor = (message.get("params") or {}).get("cursor")
            page = 1 if cursor == "page-2" else 0
            result = {"tools": self.tool_pages[page]}
            if page + 1 < len(self.tool_pages):
                result["nextCursor"] = "page-2"
            return httpx.Response(200, json=self.rpc(message["id"], result))
        if method == "tools/call":
            params = message["params"]
            return self.on_call(message["id"], params["name"], params["arguments"])
        error = {"code": -32601, "message": "Method not found"}
        return httpx.Response(200, json=self.rpc(message["id"], error=error))


def tokens(
    *,
    access: str = "access-1",
    refresh: str | None = "refresh-1",
    expires_in: float = 300.0,
    now: float | None = None,
) -> OAuthTokens:
    current = time.time() if now is None else now
    return OAuthTokens(
        access_token=access,
        refresh_token=refresh,
        expires_at=current + expires_in,
        refresh_expires_at=None,
        scope="openid profile offline_access",
        issuer=ISSUER,
        client_id=CLIENT_ID,
        resource=RESOURCE,
        token_endpoint=TOKEN_URL,
        revocation_endpoint=REVOKE_URL,
        obtained_at=current,
    )


@pytest.fixture
def fake() -> FakeMaltego:
    return FakeMaltego()


@pytest.fixture
def settings(tmp_path) -> MaltegoSettings:
    return MaltegoSettings(RESOURCE, CLIENT_ID, 0, 30.0, tmp_path / "config" / "token.json")


@pytest.fixture
def make_tokens():
    return tokens


@pytest.fixture
def signed_in(settings) -> TokenStore:
    store = TokenStore(settings.token_path)
    store.save(tokens())
    return store
