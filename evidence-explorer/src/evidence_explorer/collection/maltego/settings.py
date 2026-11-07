"""Server-side Maltego Data MCP settings.

Nothing here reads or returns a credential. The OAuth token file lives outside
the project tree by default so that a sign-in never enters version control,
case packages, or exports.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ...source_contracts import safe_source_url

DEFAULT_SERVER_URL = "https://datamcp.maltego.com/mcp"
DEFAULT_CLIENT_ID = "maltego-datamcp-mcp"
# The port documented for other MCP clients; any loopback port is accepted.
DEFAULT_CALLBACK_PORT = 33418
# Some Data MCP sources take a minute or more per call.
DEFAULT_TIMEOUT_SECONDS = 300.0
# A generic client name: no personal contact details are sent to the server.
USER_AGENT = "traced-evidence-workbench/0.1"

_CLIENT_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


class MaltegoSettingsError(ValueError):
    """A user-safe configuration problem."""


@dataclass(frozen=True)
class MaltegoSettings:
    server_url: str
    client_id: str
    callback_port: int
    timeout_seconds: float
    token_path: Path

    @property
    def origin(self) -> str:
        parsed = urlsplit(self.server_url)
        return f"{parsed.scheme}://{parsed.netloc}"


def default_token_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("XDG_CONFIG_HOME", "").strip()
    base = Path(configured).expanduser() if configured else Path.home() / ".config"
    return base / "traced" / "maltego-datamcp-token.json"


def validate_server_url(value: str) -> str:
    message = (
        "Set TRACED_MALTEGO_MCP_URL to the HTTPS Data MCP endpoint without credentials, "
        "query, or fragment."
    )
    try:
        safe_source_url(value)
    except ValueError:
        raise MaltegoSettingsError(message) from None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.query or parsed.fragment or "#" in value:
        raise MaltegoSettingsError(message)
    if not parsed.path or parsed.path == "/":
        raise MaltegoSettingsError(message)
    return value.rstrip("/")


def maltego_settings(environ: Mapping[str, str] | None = None) -> MaltegoSettings:
    """Read validated settings; raises MaltegoSettingsError with a safe message."""
    env = os.environ if environ is None else environ
    server_url = validate_server_url(
        env.get("TRACED_MALTEGO_MCP_URL", "").strip() or DEFAULT_SERVER_URL
    )

    client_id = env.get("TRACED_MALTEGO_CLIENT_ID", "").strip() or DEFAULT_CLIENT_ID
    if not _CLIENT_ID.fullmatch(client_id):
        raise MaltegoSettingsError("Set TRACED_MALTEGO_CLIENT_ID to a plain OAuth client ID.")

    raw_port = env.get("TRACED_MALTEGO_CALLBACK_PORT", "").strip()
    try:
        port = int(raw_port) if raw_port else DEFAULT_CALLBACK_PORT
        if port != 0 and not 1024 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise MaltegoSettingsError(
            "Set TRACED_MALTEGO_CALLBACK_PORT to 0 or a port between 1024 and 65535."
        ) from None

    raw_timeout = env.get("TRACED_MALTEGO_TIMEOUT_SECONDS", "").strip()
    try:
        timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
        if not 10.0 <= timeout <= 900.0:
            raise ValueError
    except ValueError:
        raise MaltegoSettingsError(
            "Set TRACED_MALTEGO_TIMEOUT_SECONDS between 10 and 900 seconds."
        ) from None

    raw_path = env.get("TRACED_MALTEGO_TOKEN_FILE", "").strip()
    token_path = Path(raw_path).expanduser() if raw_path else default_token_path(env)
    if not token_path.is_absolute():
        raise MaltegoSettingsError(
            "Set TRACED_MALTEGO_TOKEN_FILE to an absolute path outside the project folder."
        )
    return MaltegoSettings(server_url, client_id, port, timeout, token_path)
