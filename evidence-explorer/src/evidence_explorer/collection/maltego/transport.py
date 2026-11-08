"""Minimal synchronous MCP Streamable HTTP client for remote tool calls.

Speaks JSON-RPC 2.0 to one HTTPS endpoint with a bearer token: initialize,
tools/list, and tools/call. Replies may be JSON or Server-Sent Events; session
identifiers, one token refresh after HTTP 401, one re-initialization after an
expired session, and bounded stream resumption are supported. Server requests
other than ping are declined because Traced offers no sampling, roots, or
elicitation. Redirects are never followed, so the token reaches one origin only.

Tool results are returned exactly as received. Interpreting them as evidence
belongs to the record layer, which preserves them unchanged.
"""

from __future__ import annotations

import itertools
import json
import re
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from ..models import CollectionFailure
from .oauth import SIGN_IN_HINT, TokenProvider
from .settings import USER_AGENT, MaltegoSettings

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2025-11-25", "2025-06-18", "2025-03-26"})
CLIENT_NAME = "traced-evidence-workbench"
CLIENT_VERSION = "0.1.0"
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_TOOL_PAGES = 20
MAX_RESUMPTIONS = 5
MAX_NOTICES = 50

_TOOL_NAME = re.compile(r"[A-Za-z0-9_.:/-]{1,128}")
_SESSION_ID = re.compile(r"[\x21-\x7e]{1,256}")


class McpFailure(CollectionFailure):
    """A user-safe Data MCP transport or protocol problem."""


class McpRpcError(McpFailure):
    """A JSON-RPC error object returned by the Data MCP."""

    def __init__(self, code: int, message: str):
        super().__init__(f"The Data MCP returned an error ({code}): {message}")
        self.code = code


@dataclass(frozen=True)
class ServerIdentity:
    protocol_version: str
    name: str | None
    version: str | None
    instructions: str | None


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    title: str | None
    description: str
    input_schema: dict
    output_schema: dict | None
    annotations: dict

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "annotations": self.annotations,
        }


@dataclass(frozen=True)
class ToolCall:
    tool_name: str
    arguments: dict
    result: dict
    requested_at: str
    completed_at: str
    server: ServerIdentity
    notices: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Event:
    data: str
    event_id: str | None
    retry_ms: int | None


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _short(value: object, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(char for char in value if char.isprintable()).strip()
    return cleaned[:limit] or None


def _json_copy(value: object) -> object:
    """Return a detached JSON value; rejects NaN and other non-JSON content."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        raise McpFailure("Tool arguments must be plain JSON values.") from None


def _iter_lines(response: httpx.Response, deadline: float, limit: int) -> Iterator[str]:
    """Split an event stream into lines, enforcing the deadline and a byte budget."""
    pending = b""
    received = 0
    for chunk in response.iter_bytes():
        received += len(chunk)
        if received > limit:
            raise McpFailure("The Data MCP response exceeded the 16 MiB limit.")
        if time.monotonic() > deadline:
            raise _timeout_failure()
        pending += chunk
        while True:
            newline, carriage = pending.find(b"\n"), pending.find(b"\r")
            if newline == -1 and carriage == -1:
                break
            if carriage != -1 and (newline == -1 or carriage < newline):
                if carriage == len(pending) - 1:
                    break  # A CRLF pair may continue in the next chunk.
                step = 2 if pending[carriage + 1 : carriage + 2] == b"\n" else 1
                line, pending = pending[:carriage], pending[carriage + step :]
            else:
                line, pending = pending[:newline], pending[newline + 1 :]
            yield line.decode("utf-8", errors="replace")
    if pending:
        yield pending.rstrip(b"\r").decode("utf-8", errors="replace")


def _events(lines: Iterator[str]) -> Iterator[_Event]:
    data: list[str] = []
    event_id: str | None = None
    retry: int | None = None
    dispatched = True
    for line in lines:
        if not line:
            if not dispatched:
                yield _Event("\n".join(data), event_id, retry)
            data, retry, dispatched = [], None, True
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "data":
            data.append(value)
            dispatched = False
        elif name == "id" and "\0" not in value:
            event_id = value
            dispatched = False
        elif name == "retry" and value.isdigit():
            retry = int(value)
    if not dispatched:
        yield _Event("\n".join(data), event_id, retry)


def _messages(text: str) -> list:
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise McpFailure("The Data MCP sent an unreadable message.") from None
    return data if isinstance(data, list) else [data]


def _timeout_failure() -> McpFailure:
    return McpFailure(
        "The Data MCP did not finish in time. Some sources take minutes; try again later "
        "or raise TRACED_MALTEGO_TIMEOUT_SECONDS.",
        retry_after_seconds=60,
    )


def _tool(raw: object) -> ToolDescriptor | None:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
        return None
    annotations = raw.get("annotations") if isinstance(raw.get("annotations"), dict) else {}
    title = _short(raw.get("title")) or _short(annotations.get("title"))
    description = raw.get("description") if isinstance(raw.get("description"), str) else ""
    schema = raw.get("inputSchema") if isinstance(raw.get("inputSchema"), dict) else {}
    output = raw.get("outputSchema") if isinstance(raw.get("outputSchema"), dict) else None
    return ToolDescriptor(name, title, description[:8000], schema, output, annotations)


class McpSession:
    """One Data MCP session; use as a context manager so the session is closed."""

    def __init__(
        self,
        settings: MaltegoSettings,
        tokens: TokenProvider,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        self.settings = settings
        self.tokens = tokens
        self._http = httpx.Client(
            transport=transport, follow_redirects=False, headers={"User-Agent": USER_AGENT}
        )
        self._ids = itertools.count(1)
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._server: ServerIdentity | None = None
        self._last_token: str | None = None

    def __enter__(self) -> McpSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def initialize(self) -> ServerIdentity:
        if self._server is None:
            self._handshake()
        assert self._server is not None
        return self._server

    def list_tools(self) -> list[ToolDescriptor]:
        self.initialize()
        tools: list[ToolDescriptor] = []
        cursor = None
        for _ in range(MAX_TOOL_PAGES):
            result, _notices = self._rpc("tools/list", {"cursor": cursor} if cursor else None)
            raw_tools = result.get("tools")
            tools.extend(
                tool
                for tool in map(_tool, raw_tools if isinstance(raw_tools, list) else [])
                if tool
            )
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict) -> ToolCall:
        server = self.initialize()
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            raise McpFailure("Choose a Data MCP tool by its exact name.")
        if not isinstance(arguments, dict):
            raise McpFailure("Tool arguments must be a JSON object.")
        detached = _json_copy(arguments)
        requested_at = utc_now()
        result, notices = self._rpc(
            "tools/call",
            {"name": name, "arguments": detached, "_meta": {"progressToken": uuid.uuid4().hex}},
        )
        completed_at = utc_now()
        if not isinstance(result.get("content", []), list):
            raise McpFailure("The Data MCP returned an unreadable tool result.")
        return ToolCall(
            name, detached, _json_copy(result), requested_at, completed_at, server, notices
        )

    def close(self) -> None:
        if self._session_id is not None and self._last_token is not None:
            try:
                self._http.request(
                    "DELETE",
                    self.settings.server_url,
                    headers=self._headers(self._last_token, post=False),
                    timeout=5.0,
                )
            except httpx.HTTPError:
                pass
        self._session_id = None
        self._http.close()

    def _handshake(self) -> None:
        self._session_id = self._protocol_version = None
        self._server = None
        result, _notices = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )
        version = result.get("protocolVersion")
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise McpFailure("The Data MCP uses a protocol version Traced does not support.")
        capabilities = result.get("capabilities")
        if not isinstance(capabilities, dict) or "tools" not in capabilities:
            raise McpFailure("The Data MCP does not offer tools to this account.")
        info = result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {}
        instructions = result.get("instructions")
        self._protocol_version = version
        self._server = ServerIdentity(
            version,
            _short(info.get("name")),
            _short(info.get("version"), 80),
            instructions[:4000] if isinstance(instructions, str) else None,
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"}, None, [])

    def _rpc(self, method: str, params: dict | None) -> tuple[dict, tuple[str, ...]]:
        request_id = next(self._ids)
        payload: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        notices: list[str] = []
        message = self._send(payload, request_id, notices)
        if message is None:
            raise McpFailure("The Data MCP did not answer the request.")
        if "error" in message:
            error = message["error"] if isinstance(message["error"], dict) else {}
            code = error.get("code")
            raise McpRpcError(
                code if isinstance(code, int) and not isinstance(code, bool) else -32603,
                _short(error.get("message"), 300) or "no message",
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpFailure("The Data MCP returned an unreadable result.")
        return result, tuple(notices)

    def _headers(self, token: str, *, post: bool) -> dict[str, str]:
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream" if post else "text/event-stream",
        }
        if post:
            headers["Content-Type"] = "application/json"
        if self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _timeout(self, deadline: float) -> httpx.Timeout:
        remaining = max(1.0, deadline - time.monotonic())
        return httpx.Timeout(remaining, connect=min(15.0, remaining))

    def _send(self, payload: dict, request_id: int | None, notices: list[str]) -> dict | None:
        """POST one message and consume its reply; returns the matching response, if any."""
        deadline = time.monotonic() + self.settings.timeout_seconds
        rejected: str | None = None
        reinitialized = False
        while True:
            token = self.tokens.access_token(rejected=rejected)
            self._last_token = token
            retry_auth = retry_session = False
            last_event_id: str | None = None
            retry_ms: int | None = None
            try:
                with self._http.stream(
                    "POST",
                    self.settings.server_url,
                    content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    headers=self._headers(token, post=True),
                    timeout=self._timeout(deadline),
                ) as response:
                    if response.status_code == 401 and rejected is None:
                        retry_auth = True
                    elif (
                        response.status_code == 404
                        and self._session_id is not None
                        and not reinitialized
                        and payload.get("method") != "initialize"
                    ):
                        retry_session = True
                    else:
                        self._raise_for_status(response)
                        if payload.get("method") == "initialize":
                            self._capture_session(response)
                        if request_id is None:
                            return None
                        message, last_event_id, retry_ms = self._consume(
                            response, request_id, deadline, notices
                        )
                        if message is not None:
                            return message
            except httpx.TimeoutException:
                raise _timeout_failure() from None
            except httpx.HTTPError:
                raise McpFailure(
                    "The Data MCP could not be reached. Check the network connection."
                ) from None
            if retry_auth:
                rejected = token
                continue
            if retry_session:
                reinitialized = True
                self._handshake()
                continue
            return self._resume(request_id, last_event_id, retry_ms, deadline, notices)

    def _consume(
        self, response: httpx.Response, request_id: int, deadline: float, notices: list[str]
    ) -> tuple[dict | None, str | None, int | None]:
        kind = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if kind == "application/json":
            body = b"".join(_iter_raw(response, deadline))
            for message in _messages(body.decode("utf-8", errors="replace")):
                matched = self._dispatch(message, request_id, notices)
                if matched is not None:
                    return matched, None, None
            raise McpFailure("The Data MCP reply did not answer the request.")
        if kind == "text/event-stream":
            return self._read_events(response, request_id, deadline, notices)
        raise McpFailure("The Data MCP returned an unsupported response type.")

    def _read_events(
        self, response: httpx.Response, request_id: int, deadline: float, notices: list[str]
    ) -> tuple[dict | None, str | None, int | None]:
        last_event_id: str | None = None
        retry_ms: int | None = None
        for event in _events(_iter_lines(response, deadline, MAX_MESSAGE_BYTES)):
            if event.event_id is not None:
                last_event_id = event.event_id
            if event.retry_ms is not None:
                retry_ms = event.retry_ms
            if not event.data.strip():
                continue  # Priming events carry only an event ID.
            for message in _messages(event.data):
                matched = self._dispatch(message, request_id, notices)
                if matched is not None:
                    return matched, last_event_id, retry_ms
        return None, last_event_id, retry_ms

    def _resume(
        self,
        request_id: int | None,
        last_event_id: str | None,
        retry_ms: int | None,
        deadline: float,
        notices: list[str],
    ) -> dict:
        failure = McpFailure(
            "The Data MCP stream ended before the result arrived. The lookup may still have "
            "used credits; check the team balance before running it again.",
            retry_after_seconds=60,
        )
        for _ in range(MAX_RESUMPTIONS):
            if request_id is None or last_event_id is None:
                break
            delay = min((1000 if retry_ms is None else retry_ms) / 1000, 10.0)
            if time.monotonic() + delay >= deadline:
                break
            time.sleep(delay)
            token = self.tokens.access_token()
            self._last_token = token
            headers = {**self._headers(token, post=False), "Last-Event-ID": last_event_id}
            try:
                with self._http.stream(
                    "GET",
                    self.settings.server_url,
                    headers=headers,
                    timeout=self._timeout(deadline),
                ) as response:
                    kind = response.headers.get("content-type", "").partition(";")[0].strip()
                    if response.status_code != 200 or kind.lower() != "text/event-stream":
                        break
                    message, new_id, new_retry = self._read_events(
                        response, request_id, deadline, notices
                    )
            except httpx.TimeoutException:
                raise _timeout_failure() from None
            except httpx.HTTPError:
                break
            if message is not None:
                return message
            last_event_id = new_id or last_event_id
            retry_ms = retry_ms if new_retry is None else new_retry
        raise failure

    def _dispatch(self, message: object, request_id: int, notices: list[str]) -> dict | None:
        if not isinstance(message, dict):
            return None
        if "method" in message:
            if "id" in message:
                self._decline(message)
            else:
                self._note(message, notices)
            return None
        if message.get("id") == request_id and ("result" in message or "error" in message):
            return message
        return None

    def _decline(self, request: dict) -> None:
        if request.get("method") == "ping":
            reply = {"jsonrpc": "2.0", "id": request["id"], "result": {}}
        else:
            reply = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": -32601, "message": "Not supported by this client."},
            }
        try:
            self._send(reply, None, [])
        except CollectionFailure:
            pass

    @staticmethod
    def _note(message: dict, notices: list[str]) -> None:
        if len(notices) >= MAX_NOTICES:
            return
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if message.get("method") == "notifications/progress":
            text = _short(params.get("message"))
            progress, total = params.get("progress"), params.get("total")
            amount = f"{progress}/{total}" if total is not None else f"{progress}"
            notices.append(f"progress {amount}" + (f": {text}" if text else ""))
        elif message.get("method") == "notifications/message":
            data = params.get("data")
            text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            notices.append(f"{_short(params.get('level')) or 'info'}: {_short(text, 300)}")

    def _capture_session(self, response: httpx.Response) -> None:
        session = response.headers.get("mcp-session-id")
        if session is None:
            return
        if not _SESSION_ID.fullmatch(session):
            raise McpFailure("The Data MCP returned an invalid session identifier.")
        self._session_id = session

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        if 300 <= status < 400:
            raise McpFailure(
                "The Data MCP redirected the request; Traced does not send the sign-in elsewhere."
            )
        if status == 401:
            raise McpFailure("The Data MCP rejected the Maltego sign-in. " + SIGN_IN_HINT)
        detail = _error_detail(response)
        if status == 403:
            raise McpFailure(
                "The Data MCP denied access (HTTP 403)." + detail, retry_after_seconds=60
            )
        if status == 429:
            raise McpFailure(
                "The Data MCP is limiting requests. Try again later.",
                retry_after_seconds=_retry_after(response.headers.get("retry-after")),
            )
        if status >= 500:
            raise McpFailure(
                f"The Data MCP is unavailable (HTTP {status}). Try again in a minute.",
                retry_after_seconds=60,
            )
        raise McpFailure(f"The Data MCP refused the request (HTTP {status})." + detail)


def _iter_raw(response: httpx.Response, deadline: float) -> Iterator[bytes]:
    received = 0
    for chunk in response.iter_bytes():
        received += len(chunk)
        if received > MAX_MESSAGE_BYTES:
            raise McpFailure("The Data MCP response exceeded the 16 MiB limit.")
        if time.monotonic() > deadline:
            raise _timeout_failure()
        yield chunk


def _error_detail(response: httpx.Response) -> str:
    """Return a short server explanation from a JSON-RPC error body, if one is present."""
    try:
        body = b""
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > 65_536:
                return ""
        data = json.loads(body.decode("utf-8"))
    except (httpx.HTTPError, UnicodeDecodeError, ValueError, RecursionError):
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if message is None and isinstance(data, dict):
        message = data.get("detail")
    text = _short(message, 300)
    return f" {text}" if text else ""


def _retry_after(value: str | None) -> int:
    try:
        return max(1, min(int(value or "60"), 3600))
    except ValueError:
        return 60
