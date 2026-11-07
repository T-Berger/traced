"""Local storage for one person's Maltego Data MCP sign-in.

Tokens stay in a user-owned file (mode 0600, directory 0700) and never appear
in records, exports, log lines, exception messages, or object representations.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

try:  # The worker lock uses the same POSIX primitive.
    import fcntl
except ImportError:  # pragma: no cover - Windows is not a supported workspace host.
    fcntl = None

TOKEN_SCHEMA = "traced.maltego.oauth/1"
_MAX_TOKEN_FILE_BYTES = 256 * 1024


class TokenStoreError(ValueError):
    """The saved sign-in cannot be read; signing in again replaces it."""


@dataclass(frozen=True, repr=False)
class OAuthTokens:
    access_token: str
    refresh_token: str | None
    expires_at: float | None
    refresh_expires_at: float | None
    scope: str
    issuer: str
    client_id: str
    resource: str
    token_endpoint: str
    revocation_endpoint: str | None
    obtained_at: float

    def __repr__(self) -> str:
        return (
            "OAuthTokens("
            f"issuer={self.issuer!r}, resource={self.resource!r}, "
            f"expires_at={self.expires_at!r}, refreshable={self.refresh_token is not None})"
        )

    __str__ = __repr__

    def access_usable(self, now: float | None = None, *, skew: float = 60.0) -> bool:
        current = time.time() if now is None else now
        return self.expires_at is None or self.expires_at - skew > current

    def refresh_usable(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return self.refresh_token is not None and (
            self.refresh_expires_at is None or self.refresh_expires_at > current
        )

    def matches(self, *, resource: str, client_id: str) -> bool:
        return self.resource == resource and self.client_id == client_id

    def with_refresh(self, refreshed: OAuthTokens) -> OAuthTokens:
        """Keep the previous refresh token when the server does not rotate it."""
        if refreshed.refresh_token is not None:
            return refreshed
        return replace(
            refreshed,
            refresh_token=self.refresh_token,
            refresh_expires_at=self.refresh_expires_at,
        )

    def to_json(self) -> dict:
        return {
            "schema": TOKEN_SCHEMA,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "refresh_expires_at": self.refresh_expires_at,
            "scope": self.scope,
            "issuer": self.issuer,
            "client_id": self.client_id,
            "resource": self.resource,
            "token_endpoint": self.token_endpoint,
            "revocation_endpoint": self.revocation_endpoint,
            "obtained_at": self.obtained_at,
        }

    @classmethod
    def from_json(cls, data: object) -> OAuthTokens:
        message = "The saved Maltego sign-in is unreadable. Sign in again."
        if not isinstance(data, dict) or data.get("schema") != TOKEN_SCHEMA:
            raise TokenStoreError(message)

        def text(name: str, *, optional: bool = False) -> str | None:
            value = data.get(name)
            if value is None and optional:
                return None
            if not isinstance(value, str) or not value or len(value) > 65_536:
                raise TokenStoreError(message)
            return value

        def number(name: str) -> float | None:
            value = data.get(name)
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TokenStoreError(message)
            return float(value)

        obtained = number("obtained_at")
        if obtained is None:
            raise TokenStoreError(message)
        return cls(
            access_token=text("access_token"),
            refresh_token=text("refresh_token", optional=True),
            expires_at=number("expires_at"),
            refresh_expires_at=number("refresh_expires_at"),
            scope=data.get("scope") if isinstance(data.get("scope"), str) else "",
            issuer=text("issuer"),
            client_id=text("client_id"),
            resource=text("resource"),
            token_endpoint=text("token_endpoint"),
            revocation_endpoint=text("revocation_endpoint", optional=True),
            obtained_at=obtained,
        )


class TokenStore:
    """Atomic JSON file storage with an advisory lock for token refresh."""

    def __init__(self, path: Path):
        self.path = Path(path)

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> OAuthTokens | None:
        try:
            with self.path.open("rb") as handle:
                if os.fstat(handle.fileno()).st_mode & 0o077:
                    os.chmod(self.path, 0o600)
                raw = handle.read(_MAX_TOKEN_FILE_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError:
            raise TokenStoreError("The saved Maltego sign-in cannot be read.") from None
        if len(raw) > _MAX_TOKEN_FILE_BYTES:
            raise TokenStoreError("The saved Maltego sign-in is unreadable. Sign in again.")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise TokenStoreError(
                "The saved Maltego sign-in is unreadable. Sign in again."
            ) from None
        return OAuthTokens.from_json(data)

    def save(self, tokens: OAuthTokens) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(
            prefix="." + self.path.name + ".", suffix=".tmp", dir=self.path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(tokens.to_json(), handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def clear(self) -> bool:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Serialize refreshes so rotating refresh tokens are not spent twice."""
        if fcntl is None:  # pragma: no cover
            yield
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
