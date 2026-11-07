"""Root commands preserve saved-store selection without starting applications."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import traced

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INTEGRATION_MODULE = "evidence_explorer.review.integrations"


class TracedConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="traced-root-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "project with spaces"
        self.root.mkdir()
        self.member = self.root / "evidence-explorer"
        self.member.mkdir()
        self.caller = Path(temporary.name).resolve() / "unrelated caller"
        self.caller.mkdir()
        previous_cwd = Path.cwd()
        self.addCleanup(os.chdir, previous_cwd)
        os.chdir(self.caller)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def write_env(self, directory, text):
        (directory / ".env").write_text(text, encoding="utf-8")

    def assert_selection(self, selected, expected):
        self.assertEqual(Path(selected["project_root"]), self.root)
        self.assertEqual(Path(selected["environment"]), self.root / ".venv")
        self.assertEqual(Path(selected["data_dir"]), expected)
        self.assertEqual(Path(selected["database"]), expected / "evidence.sqlite3")
        self.assertTrue(selected["data_source"])
        self.assertEqual(Path(os.environ["EVIDENCE_DATA_DIR"]), expected)
        self.assertIn(os.environ["PYTHON_DOTENV_DISABLED"].lower(), {"1", "true", "yes"})

    def test_default_preserves_existing_member_database_without_creating_paths(self):
        self.write_env(self.caller, "EVIDENCE_DATA_DIR=caller-data\n")
        selected = traced.configure_environment(self.root)
        self.assert_selection(selected, self.member / "data")
        self.assertFalse((self.member / "data").exists())
        self.assertFalse((self.root / "data").exists())
        self.assertFalse((self.root / ".venv").exists())
        self.assertFalse((self.caller / "caller-data").exists())

    def test_root_dotenv_relative_selection_is_based_on_root(self):
        self.write_env(self.root, 'EVIDENCE_DATA_DIR="saved root cases"\n')
        selected = traced.configure_environment(self.root)
        self.assert_selection(selected, self.root / "saved root cases")

    def test_legacy_dotenv_keeps_member_relative_path(self):
        self.write_env(self.member, "EVIDENCE_DATA_DIR=./data/evidence-trail\n")
        selected = traced.configure_environment(self.root)
        self.assert_selection(selected, self.member / "data" / "evidence-trail")

    def test_root_dotenv_overrides_legacy_but_keeps_unset_legacy_settings(self):
        self.write_env(self.root, "EVIDENCE_DATA_DIR=./root-store\nEVIDENCE_HOST=127.0.0.2\n")
        self.write_env(
            self.member,
            "EVIDENCE_DATA_DIR=./legacy-store\nEVIDENCE_HOST=127.0.0.3\n"
            "EVIDENCE_CHAT_MODEL=legacy-model\n",
        )
        selected = traced.configure_environment(self.root)
        self.assert_selection(selected, self.root / "root-store")
        self.assertEqual(os.environ["EVIDENCE_HOST"], "127.0.0.2")
        self.assertEqual(os.environ["EVIDENCE_CHAT_MODEL"], "legacy-model")

    def test_process_environment_preserves_member_relative_meaning_and_precedence(self):
        self.write_env(self.root, "EVIDENCE_DATA_DIR=./root-store\nEVIDENCE_HOST=127.0.0.2\n")
        self.write_env(self.member, "EVIDENCE_DATA_DIR=./legacy-store\n")
        os.environ.update(EVIDENCE_DATA_DIR="./process-store", EVIDENCE_HOST="127.0.0.4")
        selected = traced.configure_environment(self.root)
        self.assert_selection(selected, self.member / "process-store")
        self.assertEqual(os.environ["EVIDENCE_HOST"], "127.0.0.4")

    def test_explicit_selection_is_root_relative_and_overrides_all_environment_sources(self):
        self.write_env(self.root, "EVIDENCE_DATA_DIR=./root-store\n")
        self.write_env(self.member, "EVIDENCE_DATA_DIR=./legacy-store\n")
        os.environ["EVIDENCE_DATA_DIR"] = "./process-store"
        selected = traced.configure_environment(self.root, "./chosen cases")
        self.assert_selection(selected, self.root / "chosen cases")
        self.assertFalse((self.root / "chosen cases").exists())

    def test_absolute_selection_and_existing_database_bytes_are_preserved(self):
        selected_directory = self.caller / "existing cases"
        selected_directory.mkdir()
        database = selected_directory / "evidence.sqlite3"
        original = b"fixture bytes: configuration must never open or initialize this file"
        database.write_bytes(original)
        os.environ["EVIDENCE_DATA_DIR"] = str(selected_directory)
        self.assert_selection(traced.configure_environment(self.root), selected_directory)
        self.assertEqual(database.read_bytes(), original)
        self.assertEqual(list(selected_directory.iterdir()), [database])

    def test_disabled_dotenv_ignores_both_files_but_preserves_process_selection(self):
        self.write_env(self.root, "EVIDENCE_DATA_DIR=./root-store\nEVIDENCE_HOST=127.0.0.2\n")
        self.write_env(self.member, "EVIDENCE_DATA_DIR=./legacy-store\n")
        for disabled in ("1", "true", "YES"):
            with (
                self.subTest(disabled=disabled),
                patch.dict(
                    os.environ,
                    {"PYTHON_DOTENV_DISABLED": disabled, "EVIDENCE_DATA_DIR": "./process-store"},
                    clear=True,
                ),
            ):
                selected = traced.configure_environment(self.root)
                self.assert_selection(selected, self.member / "process-store")
                self.assertNotIn("EVIDENCE_HOST", os.environ)

    def test_disabled_dotenv_without_selection_keeps_default(self):
        self.write_env(self.root, "EVIDENCE_DATA_DIR=./root-store\n")
        self.write_env(self.member, "EVIDENCE_DATA_DIR=./legacy-store\n")
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"
        self.assert_selection(traced.configure_environment(self.root), self.member / "data")

    def test_false_dotenv_disabled_setting_still_loads_root_configuration(self):
        self.write_env(self.root, "EVIDENCE_DATA_DIR=./root-store\n")
        os.environ["PYTHON_DOTENV_DISABLED"] = "0"
        self.assert_selection(traced.configure_environment(self.root), self.root / "root-store")

    def test_blank_selection_is_rejected_for_every_source(self):
        for source in ("argument", "process", "root", "legacy"):
            for value in ("", "   "):
                with (
                    self.subTest(source=source, value=value),
                    patch.dict(os.environ, {}, clear=True),
                ):
                    for directory in (self.root, self.member):
                        (directory / ".env").unlink(missing_ok=True)
                    argument = None
                    if source == "argument":
                        argument = value
                    elif source == "process":
                        os.environ["EVIDENCE_DATA_DIR"] = value
                    else:
                        directory = self.root if source == "root" else self.member
                        self.write_env(directory, f'EVIDENCE_DATA_DIR="{value}"\n')
                    with self.assertRaises(ValueError):
                        traced.configure_environment(self.root, argument)
                    self.assertFalse((self.root / "data").exists())
                    self.assertFalse((self.member / "data").exists())

    def capture_dispatch(self, arguments):
        captured = {}

        def run_module(module, *, run_name):
            captured.update(
                module=module,
                run_name=run_name,
                arguments=sys.argv[1:],
                cwd=Path.cwd(),
                data_dir=os.environ["EVIDENCE_DATA_DIR"],
            )

        with patch.object(traced.runpy, "run_module", side_effect=run_module) as run:
            traced.main(arguments, root=self.root)
            run.assert_called_once()
        self.assertEqual(captured["module"], INTEGRATION_MODULE)
        self.assertEqual(captured["run_name"], "__main__")
        self.assertEqual(captured["cwd"], self.root)
        self.assertEqual(Path(captured["data_dir"]), self.member / "data")
        return captured["arguments"]

    def test_config_reports_selection_without_dispatch_or_creating_store(self):
        output = io.StringIO()
        with patch.object(traced.runpy, "run_module") as run, contextlib.redirect_stdout(output):
            traced.main(["--data-dir", "./selected cases", "config"], root=self.root)
        run.assert_not_called()
        selected = json.loads(output.getvalue())
        self.assert_selection(selected, self.root / "selected cases")
        self.assertFalse((self.root / "selected cases").exists())

    def test_default_dispatch_uses_one_combined_worker_with_integrated_defaults(self):
        arguments = self.capture_dispatch([])
        self.assertEqual(arguments[0], "web")
        self.assertEqual(arguments.count("--with-worker"), 1)
        self.assertEqual(arguments[arguments.index("--host") + 1], "127.0.0.1")
        self.assertEqual(arguments[arguments.index("--port") + 1], "8053")

    def test_start_uses_root_host_and_port_and_can_disable_worker(self):
        self.write_env(self.root, "EVIDENCE_HOST=127.0.0.2\nTRACED_PORT=8097\n")
        arguments = self.capture_dispatch(["start", "--no-worker"])
        self.assertEqual(arguments[0], "web")
        self.assertNotIn("--with-worker", arguments)
        self.assertEqual(arguments[arguments.index("--host") + 1], "127.0.0.2")
        self.assertEqual(arguments[arguments.index("--port") + 1], "8097")

    def test_review_passes_case_arguments_without_adding_a_worker(self):
        forwarded = ["cases"]
        self.assertEqual(self.capture_dispatch(["review", *forwarded]), forwarded)

    def test_review_preserves_option_arguments_and_paths_containing_spaces(self):
        forwarded = ["import", "/external cases/chosen case.zip"]
        self.assertEqual(self.capture_dispatch(["review", *forwarded]), forwarded)

    def test_review_help_reaches_existing_cli(self):
        self.assertEqual(self.capture_dispatch(["review", "--help"]), ["--help"])


class TracedShellTests(unittest.TestCase):
    def test_shell_selects_root_and_shared_environment_from_unrelated_directory(self):
        with tempfile.TemporaryDirectory(prefix="traced-shell-tests-") as temporary:
            directory = Path(temporary)
            root = directory / "project with spaces"
            root.mkdir()
            launcher = root / "traced"
            shutil.copy2(PROJECT_ROOT / "traced", launcher)
            (root / "tools").mkdir()
            (root / "tools" / "traced.py").write_text("# Never executed by fake uv.\n")
            caller = directory / "unrelated caller"
            caller.mkdir()
            fake_bin = directory / "fake bin"
            fake_bin.mkdir()
            fake_uv = fake_bin / "uv"
            fake_uv.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                "print(json.dumps({'arguments': sys.argv[1:], "
                "'environment': os.environ.get('UV_PROJECT_ENVIRONMENT')}))\n"
            )
            fake_uv.chmod(0o755)
            environment = dict(os.environ)
            environment["PATH"] = str(fake_bin) + os.pathsep + os.defpath
            environment["UV_PROJECT_ENVIRONMENT"] = str(directory / "wrong environment")
            result = subprocess.run(
                ["/bin/bash", str(launcher), "--data-dir", "chosen cases", "config"],
                cwd=caller,
                env=environment,
                text=True,
                capture_output=True,
                timeout=20,
                check=True,
            )
            captured = json.loads(result.stdout)
            self.assertEqual(Path(captured["environment"]), root / ".venv")
            arguments = captured["arguments"]
            self.assertEqual(arguments[:3], ["--directory", str(root), "run"])
            for required in ("--locked", "--all-packages", "--all-extras"):
                self.assertIn(required, arguments)
            self.assertEqual(arguments[arguments.index("--group") + 1], "dev")
            self.assertEqual(
                arguments[arguments.index("python") + 1 :],
                [str(root / "tools" / "traced.py"), "--data-dir", "chosen cases", "config"],
            )
            self.assertFalse((root / ".venv").exists())
            self.assertFalse((root / "chosen cases").exists())


if __name__ == "__main__":
    unittest.main()
