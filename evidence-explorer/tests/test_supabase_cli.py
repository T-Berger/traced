"""Supabase diagnostics must not collect content or expose connection secrets."""

import json

import pytest

from evidence_explorer.collection import supabase_cli
from evidence_explorer.collection.models import CollectionFailure


@pytest.mark.parametrize("ready", [True, False])
def test_status_is_offline_and_outputs_only_readiness(monkeypatch, capsys, ready):
    monkeypatch.setattr(
        supabase_cli,
        "configuration_status",
        lambda: {"ready": ready, "message": "Settings checked.", "password": "fixture-secret"},
    )

    def unexpected_check(*_args, **_kwargs):
        pytest.fail("Status must not connect to Supabase.")

    monkeypatch.setattr(supabase_cli, "inspect_table", unexpected_check)
    assert supabase_cli.main(["status"]) == (0 if ready else 1)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ready": ready, "message": "Settings checked."}
    assert not captured.err
    assert "fixture-secret" not in captured.out


@pytest.mark.parametrize("table", [None, "archive.saved_content"])
def test_check_reports_schema_and_forwards_exact_table(monkeypatch, capsys, table):
    columns = [
        {"column_name": "id", "data_type": "uuid", "is_nullable": "NO"},
        {"column_name": "content", "data_type": "text", "is_nullable": "YES"},
    ]
    checked = []

    def inspect(locator):
        checked.append(locator)
        return columns

    monkeypatch.setattr(supabase_cli, "inspect_table", inspect)
    arguments = ["check"] if table is None else ["check", "--table", table]
    assert supabase_cli.main(arguments) == 0
    captured = capsys.readouterr()
    assert checked == [table or "public.content_items"]
    assert json.loads(captured.out) == {"status": "connected", "columns": columns}
    assert not captured.err


def test_safe_adapter_failure_is_reported_without_traceback(monkeypatch, capsys):
    def unavailable(_locator):
        raise CollectionFailure("Supabase table could not be inspected.")

    monkeypatch.setattr(supabase_cli, "inspect_table", unavailable)
    assert supabase_cli.main(["check"]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err == "Supabase table could not be inspected.\n"


@pytest.mark.parametrize("command", ["status", "check"])
def test_unexpected_errors_never_print_connection_details(monkeypatch, capsys, command):
    def fail(*_args):
        raise RuntimeError("postgresql://user:fixture-secret@example.invalid/postgres")

    monkeypatch.setattr(supabase_cli, "configuration_status", fail)
    monkeypatch.setattr(supabase_cli, "inspect_table", fail)
    assert supabase_cli.main([command]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "Supabase could not be checked." in captured.err
    assert "fixture-secret" not in captured.err
    assert "postgresql://" not in captured.err
    assert "Traceback" not in captured.err


def test_collect_is_not_a_diagnostic_command(monkeypatch):
    def unexpected_check(*_args):
        pytest.fail("An unsupported command must not reach the database.")

    monkeypatch.setattr(supabase_cli, "inspect_table", unexpected_check)
    with pytest.raises(SystemExit) as exc:
        supabase_cli.main(["collect"])
    assert exc.value.code == 2
