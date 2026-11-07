"""Exercise the real root launcher in disposable stores, without providers or models.

Run after ``make setup`` and the coordinated source freeze. Only loopback HTTP is
requested; no jobs or subscriptions are created. The installed workspace is reused
without syncing dependencies. Every child belongs to a new, owned process group.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, build_opener

MEMBERS = ("evidence-explorer", "swarm-detector", "operation-silent-storm")
FIXTURES = ("fictional-core-demo.zip", "evidence-trail-reviewed-swarm.zip")


class RuntimeCheckError(RuntimeError):
    """A runtime assertion failed; owned processes are still cleaned up."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise RuntimeCheckError(message)


def isolated_environment(parent: dict[str, str]) -> dict[str, str]:
    """Keep executable discovery while excluding caller configuration and secrets."""
    environment = {
        key: parent[key]
        for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT", "LANG", "LC_ALL", "UV_CACHE_DIR")
        if key in parent
    }
    environment.update(
        PYTHON_DOTENV_DISABLED="1",
        PYTHONUNBUFFERED="1",
        PYTHONDONTWRITEBYTECODE="1",
        UV_OFFLINE="1",
        UV_NO_SYNC="1",
        UV_NO_PROGRESS="1",
        EVIDENCE_READ_ONLY="false",
        EVIDENCE_CHAT_MODEL="",
        EVIDENCE_EMBEDDING_MODEL="",
        EVIDENCE_REVIEW_MODEL="",
        OLLAMA_BASE_URL="http://127.0.0.1:1",
        OPENROUTER_API_KEY="",
        HTTP_PROXY="http://127.0.0.1:1",
        HTTPS_PROXY="http://127.0.0.1:1",
        ALL_PROXY="http://127.0.0.1:1",
        NO_PROXY="127.0.0.1,localhost",
    )
    return environment


def source_hashes(root: Path) -> dict[str, str]:
    paths = [
        root / name
        for name in (
            "traced",
            "tools/traced.py",
            "tools/check_runtime.py",
            "pyproject.toml",
            "uv.lock",
            ".python-version",
        )
    ]
    paths += [root / member / "pyproject.toml" for member in MEMBERS]
    paths += [root / "evidence-explorer/tests/fixtures/case_packages" / name for name in FIXTURES]
    paths += [path for member in MEMBERS for path in (root / member / "src").rglob("*")]
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
        and path.name != ".DS_Store"
    }


def group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    return True


def stop_process(process: subprocess.Popen, *, grace: float = 8) -> dict:
    """Boundedly stop only the process group created for this particular child."""
    signals = []
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        process.poll()
        if not group_exists(process.pid):
            break
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        signals.append(sig.name)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            process.poll()
            if not group_exists(process.pid):
                break
            time.sleep(0.05)
    process.wait(timeout=grace)
    require(not group_exists(process.pid), f"Owned process group {process.pid} survived cleanup")
    return {"pid": process.pid, "returncode": process.returncode, "signals": signals}


def metadata(database: Path) -> dict:
    """Read worker state without creating a missing database or writing to one."""
    if not database.exists():
        return {}
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
        ).fetchone()
        if not exists:
            return {}
        return {
            key: json.loads(value) for key, value in connection.execute("SELECT * FROM metadata")
        }


def assert_idle(database: Path) -> None:
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        for table in (
            "jobs",
            "review_jobs",
            "claim_check_jobs",
            "collection_jobs",
            "feed_subscriptions",
        ):
            if table in tables:
                count = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                require(
                    count == 0, f"Unexpected work in disposable store: {table} has {count} rows"
                )


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def http_get(port: int, path: str) -> bytes:
    require(path.startswith("/") and not path.startswith("//"), "HTTP path must stay local")
    with build_opener(ProxyHandler({})).open(
        f"http://127.0.0.1:{port}{path}", timeout=2
    ) as response:
        require(response.status == 200, f"HTTP {path}: {response.status}")
        return response.read()


class RuntimeCheck:
    def __init__(self, root: Path, temporary: Path):
        self.root = root.resolve()
        self.temporary = temporary.resolve()
        self.caller = self.temporary / "unrelated caller"
        self.caller.mkdir()
        (self.caller / ".env").write_text("EVIDENCE_DATA_DIR=wrong-caller-store\n")
        self.environment = isolated_environment(dict(os.environ))
        # The Live sources panel reads sign-in state on mount. Keep that read
        # inside the disposable runtime workspace, away from a developer's token.
        self.environment["TRACED_MALTEGO_TOKEN_FILE"] = str(self.temporary / "maltego-token.json")
        self.commands: list[dict] = []
        self.cleanup: list[dict] = []

    def command(self, store: Path, *arguments: str) -> list[str]:
        store = store.resolve()
        require(store.is_relative_to(self.temporary), "Runtime store must be task-owned")
        return [str(self.root / "traced"), "--data-dir", str(store), *arguments]

    def run(self, store: Path, *arguments: str, success: bool = True) -> str:
        command = self.command(store, *arguments)
        with (
            tempfile.TemporaryFile(mode="w+") as output,
            tempfile.TemporaryFile(mode="w+") as errors,
        ):
            process = subprocess.Popen(
                command,
                cwd=self.caller,
                env=self.environment,
                stdout=output,
                stderr=errors,
                text=True,
                start_new_session=True,
            )
            try:
                process.wait(timeout=45)
            finally:
                self.cleanup.append(stop_process(process))
            output.seek(0)
            text = output.read()
            errors.seek(0)
            error_text = errors.read()
        self.commands.append({"argv": command, "returncode": process.returncode})
        require(
            (process.returncode == 0) == success,
            f"Command returned {process.returncode}: {command}\n{text[-3000:]}\n{error_text[-3000:]}",
        )
        return text if success else text + error_text

    @contextmanager
    def server(self, store: Path, *, worker: bool):
        """Retry a bounded free-port race, and always stop the owned process group."""
        for attempt in range(3):
            port = free_port()
            arguments = ["start", "--host", "127.0.0.1", "--port", str(port)]
            if not worker:
                arguments.append("--no-worker")
            command = self.command(store, *arguments)
            with tempfile.TemporaryFile(mode="w+") as output:
                process = subprocess.Popen(
                    command,
                    cwd=self.caller,
                    env=self.environment,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
                ready = False
                try:
                    deadline = time.monotonic() + 30
                    while process.poll() is None and time.monotonic() < deadline:
                        try:
                            json.loads(http_get(port, "/_dash-layout"))
                            time.sleep(0.1)
                            ready = process.poll() is None
                            break
                        except (URLError, TimeoutError, json.JSONDecodeError):
                            time.sleep(0.1)
                    if ready:
                        yield port
                        return
                finally:
                    cleanup = stop_process(process)
                    self.cleanup.append(cleanup)
                    self.commands.append({"argv": command, "returncode": process.returncode})
                    output.seek(0)
                    log = output.read()
                if not ready and ("Address already in use" in log or "is in use" in log):
                    if attempt < 2:
                        continue
                require(ready, f"Root startup failed or timed out:\n{log[-6000:]}")

    def inspect_http(self, port: int) -> dict:
        require(b"Traced" in http_get(port, "/"), "Root page lacks Traced identity")
        require(isinstance(json.loads(http_get(port, "/_dash-layout")), dict), "Invalid layout")
        require(
            isinstance(json.loads(http_get(port, "/_dash-dependencies")), list), "Invalid callbacks"
        )
        assets = self.root / "evidence-explorer/src/evidence_explorer/review/assets"
        paths = sorted(
            path.relative_to(assets).as_posix()
            for path in assets.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and path.suffix not in {".pyc", ".pyo"}
            and not any(
                part.startswith(".") or part in {"__pycache__", "cache"}
                for part in path.relative_to(assets).parts
            )
        )
        require(paths, "No public review assets found in the captured source")
        for path in paths:
            require(
                http_get(port, "/assets/" + quote(path, safe="/")) == (assets / path).read_bytes(),
                f"HTTP asset differs from the captured source: {path}",
            )
        return {"index": 200, "layout": 200, "dependencies": 200, "exact_assets": paths}

    def check(self) -> dict:
        store = self.temporary / "empty workspace"
        configuration = json.loads(self.run(store, "config"))
        require(Path(configuration["data_dir"]) == store, "Root config selected the wrong store")
        require(
            Path(configuration["project_root"]) == self.root, "Root config depends on caller cwd"
        )
        for arguments in (("--help",), ("start", "--help"), ("review", "--help")):
            require("usage:" in self.run(store, *arguments), "Help output is missing")
        require(not store.exists(), "Configuration/help created a store")

        with self.server(store, worker=False) as port:
            no_worker = self.inspect_http(port)
            require(not metadata(store / "evidence.sqlite3").get("worker_pid"), "Unexpected worker")
            require(not (store / "worker.lock").exists(), "No-worker mode acquired the worker lock")
        require(json.loads(self.run(store, "review", "cases")) == [], "Startup seeded cases")

        database = store / "evidence.sqlite3"
        with self.server(store, worker=True) as port:
            normal = self.inspect_http(port)
            before = metadata(database)
            require(before.get("worker_state") == "running", "Default start has no running worker")
            require(
                int((store / "worker.lock").read_text()) == before["worker_pid"],
                "Worker lock and database disagree about lifecycle identity",
            )
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                current = metadata(database)
                if current.get("worker_heartbeat") != before.get("worker_heartbeat"):
                    break
                time.sleep(0.1)
            require(
                current.get("worker_heartbeat") != before.get("worker_heartbeat"),
                "Worker heartbeat did not advance",
            )
            rejected = self.run(store, "review", "worker", "--once", success=False)
            require(
                "Another worker is already running" in rejected, "Duplicate worker was not rejected"
            )
            require(
                metadata(database).get("worker_pid") == before["worker_pid"],
                "Rejected worker replaced lifecycle metadata",
            )
            assert_idle(database)
        require(
            metadata(database).get("worker_state") == "stopped",
            "Worker did not finalize on shutdown",
        )
        require(
            self.cleanup[-1]["signals"] == ["SIGINT"], "Normal shutdown needed forced termination"
        )
        require(
            self.run(store, "review", "worker", "--once").strip() == "0",
            "Idle restart processed work",
        )
        require(
            metadata(database).get("worker_state") == "stopped", "One-shot worker did not finalize"
        )
        assert_idle(database)

        producer = self.temporary / "seed and import"
        seeded = json.loads(self.run(producer, "review", "seed", "--without-swarm"))
        require(seeded, "Explicit seed produced no cases")
        for index, case in enumerate(seeded):
            seed_package = self.temporary / f"seed-{index}.zip"
            self.run(producer, "review", "export", case["id"], str(seed_package))
            bundle = json.loads(package_contents(seed_package)["bundle.json"])
            require(bundle["case"]["synthetic"] is True, "Explicit seed was not synthetic")
            require(not bundle.get("swarm_context"), "--without-swarm unexpectedly ran a detector")
        repeated = json.loads(self.run(producer, "review", "seed", "--without-swarm"))
        require(seeded == repeated, "Explicit deterministic seeding was not idempotent")
        packages = []
        for name in FIXTURES:
            fixture = self.root / "evidence-explorer/tests/fixtures/case_packages" / name
            original = package_contents(fixture)
            case_id = json.loads(original["bundle.json"])["case"]["id"]
            require(
                self.run(producer, "review", "import", str(fixture)).strip() == case_id,
                "CLI import returned a different case",
            )
            exported = self.temporary / ("exported-" + name)
            self.run(producer, "review", "export", case_id, str(exported))
            first = package_contents(exported)
            assert_evidence_preserved(original, first)
            recipient = self.temporary / ("recipient-" + fixture.stem)
            require(
                self.run(recipient, "review", "import", str(exported)).strip() == case_id,
                "Recipient import changed case identity",
            )
            reopened = self.temporary / ("reopened-" + name)
            self.run(recipient, "review", "export", case_id, str(reopened))
            require(
                first == package_contents(reopened), "Recipient export changed package contents"
            )
            assert_idle(recipient / "evidence.sqlite3")
            packages.append(
                {
                    "fixture": name,
                    "case_id": case_id,
                    "sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
                }
            )
        require(not (self.caller / "wrong-caller-store").exists(), "Caller dotenv was consumed")
        return {
            "configuration": configuration,
            "no_worker_http": no_worker,
            "combined_worker_http": normal,
            "worker_lock_rejected_duplicate": True,
            "heartbeat_advanced": True,
            "graceful_shutdown_and_reacquisition": True,
            "explicit_seed_idempotent": True,
            "roundtrips": packages,
        }


def package_contents(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def assert_evidence_preserved(original: dict[str, bytes], exported: dict[str, bytes]) -> None:
    """Allow packaging metadata evolution while preserving exact native evidence."""
    before, after = (json.loads(value["bundle.json"]) for value in (original, exported))
    for key in ("case", "records", "fragments", "findings", "links", "runs", "reviews"):
        require(before[key] == after[key], f"CLI export changed native {key}")
    for name, content in original.items():
        if name not in {"bundle.json", "manifest.json"}:
            require(exported.get(name) == content, f"CLI export changed media {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="Write the verification result as JSON")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    require((root / ".venv/bin/python").exists(), "Run make setup first; root .venv is required")
    before = source_hashes(root)
    report = {"status": "failed", "source_hashes": before, "root": str(root)}
    temporary = None
    try:
        with tempfile.TemporaryDirectory(prefix="traced-root-runtime-") as temporary:
            runner = RuntimeCheck(root, Path(temporary))
            try:
                report["checks"] = runner.check()
                require(before == source_hashes(root), "Source changed during runtime verification")
                report["status"] = "passed"
            finally:
                report.update(commands=runner.commands, cleanup=runner.cleanup)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["temporary_workspaces_removed"] = (
            temporary is not None and not Path(temporary).exists()
        )
        report["scope"] = (
            "Real root launcher, loopback HTTP, idle combined worker, deterministic synthetic "
            "seed and saved-package roundtrips. No browser, provider, model, or detector run."
        )
        emit_report(report, args.report)


def emit_report(report: dict, destination: Path | None) -> None:
    """Persist full success/failure evidence even when the output directory is new."""
    if destination:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, indent=2) + "\n")
    summary = {
        key: report[key]
        for key in ("status", "error", "checks", "temporary_workspaces_removed")
        if key in report
    }
    summary.update(
        report=str(destination.resolve()) if destination else None,
        source_files=len(report.get("source_hashes", {})),
        cleaned_process_groups=len(report.get("cleanup", [])),
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
