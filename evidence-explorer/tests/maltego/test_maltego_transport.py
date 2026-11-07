"""The MCP client speaks Streamable HTTP and keeps the sign-in on one origin."""

import json
import time

import httpx
import pytest

from evidence_explorer.collection.maltego import transport
from evidence_explorer.collection.maltego.oauth import TokenProvider
from evidence_explorer.collection.maltego.transport import McpFailure, McpRpcError, McpSession


@pytest.fixture
def connect(fake, settings, signed_in):
    def make():
        provider = TokenProvider(settings, signed_in, transport=fake.transport)
        return McpSession(settings, provider, transport=fake.transport)

    return make


def posted_methods(fake):
    return [
        json.loads(request.content).get("method")
        for request in fake.mcp_requests()
        if request.method == "POST"
    ]


def progress(value, total=None, message=None):
    params = {"progressToken": "p", "progress": value}
    if total is not None:
        params["total"] = total
    if message is not None:
        params["message"] = message
    return {"jsonrpc": "2.0", "method": "notifications/progress", "params": params}


def test_session_negotiates_the_version_pages_tools_and_closes(fake, connect):
    with connect() as mcp:
        server = mcp.initialize()
        tools = mcp.list_tools()
    assert (server.name, server.version, server.protocol_version) == (
        "Fixture Data MCP",
        "9.9",
        "2025-06-18",
    )
    assert server.instructions.startswith("Search first")
    assert [tool.name for tool in tools] == ["maltego_search", "maltego_describe", "maltego_run"]
    requests = fake.mcp_requests()
    first, later = requests[0], requests[1:]
    assert "mcp-protocol-version" not in first.headers and "mcp-session-id" not in first.headers
    assert first.headers["accept"] == "application/json, text/event-stream"
    assert all(request.headers["mcp-session-id"] == "session-1" for request in later)
    assert all(request.headers["mcp-protocol-version"] == "2025-06-18" for request in later)
    assert posted_methods(fake) == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/list",
    ]
    assert later[-1].method == "DELETE"
    assert all(request.headers["authorization"] == "Bearer access-1" for request in requests)
    assert {request.url.host for request in fake.requests} == {"datamcp.example"}


def test_tool_call_reads_events_answers_ping_declines_requests_and_keeps_progress(fake, connect):
    calls = []

    def respond(request_id, name, arguments):
        calls.append((name, arguments))
        return fake.sse(
            ("e1", ""),
            ("e2", progress(1, 2, "Querying")),
            ("e3", {"jsonrpc": "2.0", "id": "server-1", "method": "ping"}),
            ("e4", {"jsonrpc": "2.0", "id": "server-2", "method": "sampling/createMessage"}),
            ("e5", fake.rpc(request_id, fake.text_result("Registrant: Example Org"))),
        )

    fake.on_call = respond
    with connect() as mcp:
        result = mcp.call_tool("maltego_run", {"query": "example.org"})
    assert result.result == fake.text_result("Registrant: Example Org")
    assert result.notices == ("progress 1/2: Querying",)
    assert calls == [("maltego_run", {"query": "example.org"})]
    assert fake.replies == [
        {"jsonrpc": "2.0", "id": "server-1", "result": {}},
        {
            "jsonrpc": "2.0",
            "id": "server-2",
            "error": {"code": -32601, "message": "Not supported by this client."},
        },
    ]
    call = next(
        json.loads(request.content)
        for request in fake.mcp_requests()
        if request.method == "POST" and json.loads(request.content).get("method") == "tools/call"
    )
    assert call["params"]["_meta"]["progressToken"]
    assert result.requested_at.endswith("Z") and result.completed_at >= result.requested_at


def test_a_rejected_token_is_refreshed_once_and_the_request_retried(fake, connect):
    fake.valid_access = {"access-2"}
    with connect() as mcp:
        mcp.initialize()
    authorizations = [request.headers["authorization"] for request in fake.mcp_requests()]
    assert authorizations[:2] == ["Bearer access-1", "Bearer access-2"]


def test_a_second_rejection_after_refresh_asks_for_a_new_sign_in(fake, connect):
    fake.reject_all = True
    with connect() as mcp, pytest.raises(McpFailure, match="rejected the Maltego sign-in"):
        mcp.initialize()
    assert len(fake.mcp_requests()) == 2


def test_an_expired_session_is_reinitialized_once(fake, connect):
    with connect() as mcp:
        mcp.initialize()
        fake.active_sessions.clear()
        tools = mcp.list_tools()
    assert fake.initialized == 2 and len(tools) == 3


def test_a_dropped_stream_resumes_with_the_last_event_id(fake, connect):
    state = {}

    def respond(request_id, name, arguments):
        state["id"] = request_id
        body = f"retry: 0\nid: ev-1\ndata: {json.dumps(progress(1))}\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    def resume(request):
        state["last"] = request.headers.get("last-event-id")
        return fake.sse(("ev-2", fake.rpc(state["id"], fake.text_result("Late result"))))

    fake.on_call, fake.on_get = respond, resume
    with connect() as mcp:
        result = mcp.call_tool("maltego_run", {"query": "x"})
    assert result.result == fake.text_result("Late result")
    assert state["last"] == "ev-1" and result.notices == ("progress 1",)


def test_a_stream_without_result_or_event_id_warns_about_credits(fake, connect):
    fake.on_call = lambda request_id, name, arguments: fake.sse((None, progress(1)))
    with connect() as mcp, pytest.raises(McpFailure, match="may still have used credits"):
        mcp.call_tool("maltego_run", {})


def test_redirects_are_never_followed_with_the_sign_in(fake, connect, monkeypatch):
    def moved(_request):
        return httpx.Response(307, headers={"location": "https://elsewhere.example/mcp"})

    monkeypatch.setattr(fake, "mcp", moved)
    with connect() as mcp, pytest.raises(McpFailure, match="redirected"):
        mcp.initialize()
    assert {request.url.host for request in fake.requests} == {"datamcp.example"}


def test_an_unsupported_protocol_version_is_refused(fake, connect):
    fake.protocol = "2024-11-05"
    with connect() as mcp, pytest.raises(McpFailure, match="protocol version"):
        mcp.initialize()


def test_rpc_errors_keep_their_code_and_message(fake, connect):
    error = {"code": -32602, "message": "Unknown tool: nope"}
    fake.on_call = lambda request_id, name, arguments: httpx.Response(
        200, json=fake.rpc(request_id, error=error)
    )
    with connect() as mcp, pytest.raises(McpRpcError) as caught:
        mcp.call_tool("nope", {})
    assert caught.value.code == -32602 and "Unknown tool: nope" in str(caught.value)


@pytest.mark.parametrize(
    "name, arguments", [("bad name!", {}), ("maltego_run", {"value": float("nan")})]
)
def test_invalid_requests_never_reach_the_server(fake, connect, name, arguments):
    with connect() as mcp:
        mcp.initialize()
        before = len(fake.mcp_requests())
        with pytest.raises(McpFailure):
            mcp.call_tool(name, arguments)
        assert len(fake.mcp_requests()) == before


def test_oversized_streams_are_cut_off(fake, connect, monkeypatch):
    monkeypatch.setattr(transport, "MAX_MESSAGE_BYTES", 200)
    fake.on_call = lambda request_id, name, arguments: fake.sse(
        ("e1", fake.rpc(request_id, fake.text_result("x" * 500)))
    )
    with connect() as mcp, pytest.raises(McpFailure, match="limit"):
        mcp.call_tool("maltego_run", {})


def test_the_line_reader_enforces_the_deadline():
    response = httpx.Response(200, content=b"data: x\n\n")
    with pytest.raises(McpFailure, match="did not finish in time"):
        list(transport._iter_lines(response, time.monotonic() - 1, 1000))


def test_the_event_parser_handles_crlf_cr_comments_and_multiline_data():
    content = b': keepalive\r\nid: 7\rdata: {"a":\ndata: 1}\r\n\r\ndata: tail'
    lines = transport._iter_lines(httpx.Response(200, content=content), time.monotonic() + 5, 9999)
    events = [(event.data, event.event_id) for event in transport._events(lines)]
    assert events == [('{"a":\n1}', "7"), ("tail", "7")]
