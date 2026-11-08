"""Settings stay validated, and the saved sign-in stays private to one user."""

import json
import os
import stat
from dataclasses import replace

import pytest

from evidence_explorer.collection.maltego.oauth import sign_in_status
from evidence_explorer.collection.maltego.settings import (
    DEFAULT_CALLBACK_PORT,
    DEFAULT_CLIENT_ID,
    DEFAULT_SERVER_URL,
    MaltegoSettingsError,
    maltego_settings,
)
from evidence_explorer.collection.maltego.tokens import TokenStore, TokenStoreError


def test_defaults_name_the_documented_endpoint_and_keep_tokens_outside_the_project(tmp_path):
    settings = maltego_settings({"XDG_CONFIG_HOME": str(tmp_path / "xdg")})
    assert settings.server_url == DEFAULT_SERVER_URL == "https://datamcp.maltego.com/mcp"
    assert settings.client_id == DEFAULT_CLIENT_ID == "maltego-datamcp-mcp"
    assert settings.callback_port == DEFAULT_CALLBACK_PORT == 33418
    assert settings.timeout_seconds == 300.0
    assert settings.token_path == tmp_path / "xdg" / "traced" / "maltego-datamcp-token.json"
    assert settings.origin == "https://datamcp.maltego.com"


@pytest.mark.parametrize(
    "url",
    [
        "http://datamcp.maltego.com/mcp",
        "https://datamcp.maltego.com/mcp?scope=x",
        "https://user:secret@datamcp.maltego.com/mcp",
        "https://127.0.0.1/mcp",
        "https://datamcp.internal/mcp",
        "https://datamcp.maltego.com",
    ],
)
def test_unsafe_endpoints_are_rejected(url):
    with pytest.raises(MaltegoSettingsError):
        maltego_settings({"TRACED_MALTEGO_MCP_URL": url})


@pytest.mark.parametrize(
    "name, value",
    [
        ("TRACED_MALTEGO_CALLBACK_PORT", "80"),
        ("TRACED_MALTEGO_CALLBACK_PORT", "port"),
        ("TRACED_MALTEGO_TIMEOUT_SECONDS", "5"),
        ("TRACED_MALTEGO_CLIENT_ID", "client id with spaces"),
        ("TRACED_MALTEGO_TOKEN_FILE", "relative/token.json"),
    ],
)
def test_invalid_overrides_fail_with_a_setup_message(name, value):
    with pytest.raises(MaltegoSettingsError, match=name):
        maltego_settings({name: value})


def test_token_file_is_private_atomic_and_never_shows_secrets(settings, make_tokens):
    store = TokenStore(settings.token_path)
    store.save(make_tokens())
    assert stat.S_IMODE(settings.token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.token_path.parent.stat().st_mode) == 0o700
    assert not list(settings.token_path.parent.glob("*.tmp"))
    loaded = store.load()
    assert loaded.access_token == "access-1" and loaded.refresh_token == "refresh-1"
    assert "access-1" not in repr(loaded) + str(loaded)
    assert "refresh-1" not in repr(loaded) + str(loaded)


def test_loose_permissions_are_tightened_when_the_file_is_read(settings, make_tokens):
    store = TokenStore(settings.token_path)
    store.save(make_tokens())
    os.chmod(settings.token_path, 0o644)
    store.load()
    assert stat.S_IMODE(settings.token_path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"schema": "another/1"}),
        json.dumps({"schema": "traced.maltego.oauth/1", "access_token": 3}),
    ],
)
def test_unreadable_sign_in_is_reported_rather_than_used(settings, content):
    settings.token_path.parent.mkdir(parents=True)
    settings.token_path.write_text(content)
    with pytest.raises(TokenStoreError):
        TokenStore(settings.token_path).load()
    assert sign_in_status(settings).state == "unreadable"


def test_status_distinguishes_signed_out_signed_in_expired_and_foreign(settings, make_tokens):
    store = TokenStore(settings.token_path)
    assert sign_in_status(settings, store).state == "signed_out"
    store.save(make_tokens())
    status = sign_in_status(settings, store)
    assert status.ready and status.refreshable and status.access_expires_at is not None
    store.save(make_tokens(refresh=None, expires_in=-60))
    assert sign_in_status(settings, store).state == "expired"
    store.save(make_tokens())
    elsewhere = replace(settings, server_url="https://other.example/mcp")
    assert sign_in_status(elsewhere, store).state == "signed_out"
