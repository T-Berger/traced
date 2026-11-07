"""User-facing runtime failures should be concise and leave sources untouched."""

from silent_storm.cli import main


def test_unusable_database_parent_returns_concise_error(tmp_path, capsys):
    blocker = tmp_path / "ordinary-file"
    blocker.write_text("preserve this input")
    code = main(["stats", "--db", str(blocker / "store.sqlite")])
    assert code == 1
    assert "Unable to complete the request:" in capsys.readouterr().err
    assert blocker.read_text() == "preserve this input"


def test_out_of_range_port_fails_without_starting_server(tmp_path, capsys):
    code = main(
        [
            "serve",
            "--db",
            str(tmp_path / "store.sqlite"),
            "--no-import",
            "--port",
            "70000",
        ]
    )
    assert code == 1
    assert "Port must be between 1 and 65535" in capsys.readouterr().err
