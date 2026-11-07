"""Failure and isolation regressions for the real-launcher smoke harness."""

from __future__ import annotations

import io
import json
import os
import signal
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools import check_runtime as runtime


class RuntimeHarnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="runtime-harness-tests-")
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name).resolve()
        self.root = self.temporary / "project with spaces"
        self.root.mkdir()
        self.sandbox = self.temporary / "owned sandbox"
        self.sandbox.mkdir()
        self.runner = runtime.RuntimeCheck(self.root, self.sandbox)

    def test_environment_excludes_credentials_data_and_python_injection(self):
        environment = runtime.isolated_environment(
            {
                "PATH": "/executables",
                "HOME": "/user-home",
                "OPENROUTER_API_KEY": "secret",
                "EVIDENCE_DATA_DIR": "/operational",
                "PORT": "42",
                "PYTHONPATH": "/injected",
                "PYTHONHOME": "/wrong-python",
                "VIRTUAL_ENV": "/old-env",
                "OTHER_TOKEN": "secret",
            }
        )
        self.assertEqual(environment["PATH"], "/executables")
        self.assertEqual(environment["HOME"], "/user-home")
        for name in (
            "EVIDENCE_DATA_DIR",
            "PORT",
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
            "OTHER_TOKEN",
        ):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["OPENROUTER_API_KEY"], "")
        self.assertEqual(environment["PYTHON_DOTENV_DISABLED"], "1")
        self.assertEqual(environment["UV_NO_SYNC"], "1")
        self.assertEqual(environment["UV_OFFLINE"], "1")

    def test_command_rejects_a_store_outside_owned_temporary_directory(self):
        for path in (self.root / "operational data", self.sandbox / ".." / "escaped"):
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(runtime.RuntimeCheckError, "task-owned"),
            ):
                self.runner.command(path, "config")
        command = self.runner.command(self.sandbox / "store with spaces", "config")
        self.assertEqual(
            command,
            [
                str(self.root / "traced"),
                "--data-dir",
                str(self.sandbox / "store with spaces"),
                "config",
            ],
        )

    def test_metadata_read_never_creates_a_missing_database(self):
        database = self.sandbox / "absent.sqlite3"
        self.assertEqual(runtime.metadata(database), {})
        self.assertFalse(database.exists())

    def test_idle_check_rejects_queued_work_instead_of_processing_it(self):
        database = self.sandbox / "fixture.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.executescript("CREATE TABLE jobs(id INTEGER); INSERT INTO jobs VALUES(1);")
        before = database.read_bytes()
        with self.assertRaisesRegex(runtime.RuntimeCheckError, "jobs has 1 rows"):
            runtime.assert_idle(database)
        self.assertEqual(before, database.read_bytes())

    def test_timeout_always_cleans_up_the_owned_process(self):
        process = Mock(pid=321, returncode=None)
        process.wait.side_effect = subprocess.TimeoutExpired("fixture", 45)
        with (
            patch.object(runtime.subprocess, "Popen", return_value=process) as launch,
            patch.object(runtime, "stop_process", return_value={"pid": 321}) as stop,
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.runner.run(self.sandbox / "empty", "config")
        stop.assert_called_once_with(process)
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        self.assertEqual(launch.call_args.kwargs["cwd"], self.runner.caller)
        self.assertEqual(self.runner.cleanup, [{"pid": 321}])

    def test_cleanup_escalates_only_the_owned_process_group(self):
        process = Mock(pid=321, returncode=-9)
        with (
            patch.object(runtime, "group_exists", side_effect=[True, True, True, False]),
            patch.object(runtime.os, "killpg") as kill,
        ):
            result = runtime.stop_process(process, grace=0)
        self.assertEqual(
            kill.call_args_list,
            [
                unittest.mock.call(321, value)
                for value in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL)
            ],
        )
        self.assertEqual(result["signals"], ["SIGINT", "SIGTERM", "SIGKILL"])

    def test_cleanup_does_not_signal_an_already_reaped_group(self):
        process = Mock(pid=321, returncode=0)
        with (
            patch.object(runtime, "group_exists", return_value=False),
            patch.object(runtime.os, "killpg") as kill,
        ):
            result = runtime.stop_process(process)
        kill.assert_not_called()
        self.assertEqual(result["signals"], [])

    def test_cleanup_reports_a_group_that_survives_forced_termination(self):
        process = Mock(pid=321, returncode=0)
        with (
            patch.object(runtime, "group_exists", return_value=True),
            patch.object(runtime.os, "killpg"),
        ):
            with self.assertRaisesRegex(runtime.RuntimeCheckError, "survived cleanup"):
                runtime.stop_process(process, grace=0)

    def test_server_stops_on_failed_http_inspection(self):
        process = Mock(pid=321, returncode=0)
        process.poll.return_value = None
        with (
            patch.object(runtime.subprocess, "Popen", return_value=process),
            patch.object(runtime, "free_port", return_value=54321),
            patch.object(runtime, "http_get", return_value=b"{}"),
            patch.object(runtime.time, "sleep"),
            patch.object(runtime, "stop_process", return_value={"pid": 321}) as stop,
        ):
            with self.assertRaisesRegex(runtime.RuntimeCheckError, "asset mismatch"):
                with self.runner.server(self.sandbox / "empty", worker=False):
                    raise runtime.RuntimeCheckError("asset mismatch")
        stop.assert_called_once_with(process)

    def test_port_collision_retries_after_cleaning_up_first_process(self):
        first, second = Mock(pid=321, returncode=1), Mock(pid=322, returncode=0)
        first.poll.return_value = 1
        second.poll.return_value = None

        def launch(*args, **kwargs):
            del args
            if launch.calls == 0:
                kwargs["stdout"].write("Address already in use\n")
                launch.calls += 1
                return first
            return second

        launch.calls = 0
        with (
            patch.object(runtime.subprocess, "Popen", side_effect=launch),
            patch.object(runtime, "free_port", side_effect=[54321, 54322]),
            patch.object(runtime, "http_get", return_value=b"{}"),
            patch.object(runtime.time, "sleep"),
            patch.object(runtime, "stop_process", return_value={}) as stop,
        ):
            with self.runner.server(self.sandbox / "empty", worker=False) as port:
                self.assertEqual(port, 54322)
                self.assertEqual(stop.call_args_list, [unittest.mock.call(first)])
        self.assertEqual(
            stop.call_args_list, [unittest.mock.call(first), unittest.mock.call(second)]
        )

    def test_archive_check_rejects_evidence_or_media_mutation(self):
        bundle = {
            key: [] for key in ("records", "fragments", "findings", "links", "runs", "reviews")
        }
        bundle["case"] = {"id": "fixture", "synthetic": True}
        original = {"bundle.json": json.dumps(bundle).encode(), "frames/card.svg": b"shapes"}
        for altered in (
            {**original, "frames/card.svg": b"different shapes"},
            {
                **original,
                "bundle.json": json.dumps({**bundle, "records": [{"id": "changed"}]}).encode(),
            },
        ):
            with self.subTest(altered=altered), self.assertRaises(runtime.RuntimeCheckError):
                runtime.assert_evidence_preserved(original, altered)

    def test_http_inspection_covers_new_nested_public_assets_and_ignores_local_files(self):
        assets = self.root / "evidence-explorer/src/evidence_explorer/review/assets"
        public = {
            "page_navigation.css": b"new visibility rules",
            "traced/fonts/Font #1.ttf": b"font bytes",
            "traced/fonts/LICENSE.txt": b"font license",
        }
        ignored = {
            ".DS_Store",
            ".private/hidden.css",
            "cache/old.css",
            "__pycache__/old.pyc",
            "loose.pyc",
        }
        for name, content in {**public, **dict.fromkeys(ignored, b"not public")}.items():
            target = assets / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (assets / "linked.css").symlink_to(assets / "page_navigation.css")
        responses = {"/": b"Traced", "/_dash-layout": b"{}", "/_dash-dependencies": b"[]"}
        responses.update(
            {"/assets/" + runtime.quote(name, safe="/"): value for name, value in public.items()}
        )
        with patch.object(
            runtime, "http_get", side_effect=lambda _port, path: responses[path]
        ) as get:
            report = self.runner.inspect_http(54321)
        self.assertEqual(report["exact_assets"], sorted(public))
        self.assertEqual({call.args[1] for call in get.call_args_list}, set(responses))

    def test_http_inspection_rejects_changed_navigation_css(self):
        css = (
            self.root / "evidence-explorer/src/evidence_explorer/review/assets/page_navigation.css"
        )
        css.parent.mkdir(parents=True)
        css.write_bytes(b"current visibility rules")
        responses = {
            "/": b"Traced",
            "/_dash-layout": b"{}",
            "/_dash-dependencies": b"[]",
            "/assets/page_navigation.css": b"stale visibility rules",
        }
        with patch.object(runtime, "http_get", side_effect=lambda _port, path: responses[path]):
            with self.assertRaisesRegex(runtime.RuntimeCheckError, "page_navigation.css"):
                self.runner.inspect_http(54321)

    def test_http_inspection_rejects_missing_asset_tree(self):
        responses = {"/": b"Traced", "/_dash-layout": b"{}", "/_dash-dependencies": b"[]"}
        with patch.object(runtime, "http_get", side_effect=lambda _port, path: responses[path]):
            with self.assertRaisesRegex(runtime.RuntimeCheckError, "No public review assets"):
                self.runner.inspect_http(54321)

    def test_source_hashes_detect_asset_and_module_changes_without_cache_noise(self):
        for name in ("traced", "tools/traced.py", "pyproject.toml", "uv.lock"):
            target = self.root / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(b"fixture")
        asset = self.root / "evidence-explorer/src/package/assets/theme.css"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"original")
        before = runtime.source_hashes(self.root)
        cache = self.root / "evidence-explorer/src/package/__pycache__/module.pyc"
        cache.parent.mkdir()
        cache.write_bytes(os.urandom(4))
        (asset.parent / ".DS_Store").write_bytes(os.urandom(4))
        self.assertEqual(before, runtime.source_hashes(self.root))
        asset.write_bytes(b"changed")
        self.assertNotEqual(before, runtime.source_hashes(self.root))

    def test_failed_report_creates_missing_parent_and_preserves_diagnostics(self):
        destination = self.sandbox / "new dist" / "nested" / "runtime-check.json"
        report = {
            "status": "failed",
            "error": "Fixture startup failed",
            "cleanup": [{"pid": 321}],
            "source_hashes": {"fixture.py": "captured"},
            "temporary_workspaces_removed": True,
        }
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            runtime.emit_report(report, destination)
        self.assertEqual(json.loads(destination.read_text()), report)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["error"], report["error"])
        self.assertEqual(summary["report"], str(destination))
        self.assertEqual(summary["cleaned_process_groups"], 1)
        self.assertNotIn("source_hashes", summary)


if __name__ == "__main__":
    unittest.main()
