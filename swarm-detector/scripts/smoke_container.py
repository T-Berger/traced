#!/usr/bin/env python3
"""Check an already-built image using disposable synthetic storage and no host port."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import secrets
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

HTTP_PROBE = """
import base64, hashlib, json, os, urllib.error, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
base = 'http://127.0.0.1:8050'
with opener.open(base + '/health', timeout=3) as response:
    health = json.load(response)
    if response.status != 200 or health != {'ready': True, 'status': 'ok'}:
        raise AssertionError('Health readiness failed.')
token = base64.b64encode((os.environ['SWARM_BASIC_AUTH_USER'] + ':' + os.environ['SWARM_BASIC_AUTH_PASSWORD']).encode()).decode()
asset = None
for path in ('/', '/assets/workbench.css'):
    try:
        opener.open(base + path, timeout=3)
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
    else:
        raise AssertionError('Application route accepted an unauthenticated request.')
    request = urllib.request.Request(base + path, headers={'Authorization': 'Basic ' + token})
    with opener.open(request, timeout=3) as response:
        payload = response.read()
        if response.status != 200 or len(payload) < 100:
            raise AssertionError('Authenticated page or asset failed.')
        if path.endswith('.css'):
            asset = hashlib.sha256(payload).hexdigest()
print(json.dumps({'health': health, 'unauthenticated_application_status': 401,
                  'authenticated_page_and_css_status': 200, 'css_sha256': asset}))
"""

SAVE_PROBE = """
import json, os, platform
from swarm_detector.service import Workbench
wb = Workbench()
overview = wb.overview()
if not overview['synthetic'] or os.getuid() == 0:
    raise AssertionError('Smoke requires synthetic data and non-root execution.')
case = wb.list_cases(kind='candidate', run_id=overview['run_id'])[0]
review = wb.save_review(case['case_id'],
    reviewer='AUTOMATED CONTAINER FIXTURE CHECK — NOT A HUMAN REVIEW',
    decision='Insufficient evidence',
    reason='Technical container restart and backup probe; no human judgment is recorded.',
    run_id=overview['run_id'], expected_revision=0)
print(json.dumps({'run_id': overview['run_id'], 'case_id': case['case_id'],
                  'revision': review['revision'], 'message_count': overview['message_count'],
                  'runtime_uid': os.getuid(), 'runtime_python': platform.python_version()}))
"""

RESTORE_PROBE = """
import json, sqlite3
from swarm_detector.service import Workbench
wb = Workbench()
overview = wb.overview()
case = wb.list_cases(kind='candidate', run_id=overview['run_id'])[0]
review = wb.get_review(case['case_id'], run_id=overview['run_id'])
restored = Workbench('/var/data/smoke-backup.db')
backup_overview = restored.overview()
backup_review = restored.get_review(case['case_id'], run_id=overview['run_id'])
with sqlite3.connect('/var/data/smoke-backup.db') as connection:
    integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
if integrity != 'ok' or review is None or backup_review != review:
    raise AssertionError('Backup integrity or review persistence failed.')
if backup_overview['run_id'] != overview['run_id']:
    raise AssertionError('Backup reopened a different saved run.')
print(json.dumps({'run_id': overview['run_id'], 'case_id': case['case_id'],
                  'revision': review['revision'], 'message_count': overview['message_count'],
                  'sqlite_integrity': integrity, 'backup_review_matches': True}))
"""


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, text=True, capture_output=True, timeout=45)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Command failed.")
    return result


def smoke_image(image: str) -> dict:
    image_info = json.loads(_run(["docker", "image", "inspect", image]).stdout)[0]
    suffix = uuid.uuid4().hex[:12]
    name = "swarm-delivery-smoke-" + suffix
    volume = name + "-data"
    started = time.perf_counter()
    report = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "scope": "Disposable synthetic container only; no host port, host dataset mount, real data, or human review.",
        "image_tag": image,
        "image_id": image_info["Id"],
        "configured_user": image_info["Config"]["User"],
        "host": {"platform": platform.platform(), "python": platform.python_version()},
        "human_reviews_completed": 0,
        "all_checks_passed": False,
        "checks": {},
    }

    def execute_json(code: str) -> dict:
        return json.loads(_run(["docker", "exec", name, "python", "-c", code]).stdout)

    def wait_ready():
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                return execute_json(HTTP_PROBE)
            except (RuntimeError, json.JSONDecodeError):
                time.sleep(0.5)
        raise RuntimeError("Container did not pass readiness/auth/asset checks within 40 seconds.")

    try:
        _run(["docker", "volume", "create", volume])
        _run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                name,
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--env",
                "PORT=8050",
                "--env",
                "SWARM_DATA_DIR=/var/data",
                "--env",
                "SWARM_DB_PATH=/var/data/swarm.db",
                "--env",
                "SWARM_BASIC_AUTH_USER=fixture-smoke",
                "--env",
                "SWARM_BASIC_AUTH_PASSWORD=" + secrets.token_urlsafe(24),
                "--mount",
                f"type=volume,source={volume},target=/var/data",
                image_info["Id"],
            ]
        )
        http = wait_ready()
        local_css = PROJECT_ROOT / "src" / "swarm_detector" / "assets" / "workbench.css"
        if http["css_sha256"] != hashlib.sha256(local_css.read_bytes()).hexdigest():
            raise AssertionError("The image's served CSS differs from the current source.")
        report["checks"]["health_auth_and_packaged_css"] = {"passed": True, **http}
        saved = execute_json(SAVE_PROBE)
        report["checks"]["synthetic_nonroot_review_probe"] = {"passed": True, **saved}
        _run(["docker", "exec", name, "swarm-detector", "backup", "/var/data/smoke-backup.db"])
        _run(["docker", "restart", name])
        wait_ready()
        recovered = execute_json(RESTORE_PROBE)
        for key in ("run_id", "case_id", "revision", "message_count"):
            if recovered[key] != saved[key]:
                raise AssertionError("Restart or backup changed saved " + key)
        report["checks"]["restart_and_sqlite_backup"] = {"passed": True, **recovered}
        report["all_checks_passed"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        removed_container = _run(["docker", "rm", "--force", name], check=False)
        removed_volume = _run(["docker", "volume", "rm", volume], check=False)
        report["cleanup"] = {
            "container_removed": removed_container.returncode == 0,
            "volume_removed": removed_volume.returncode == 0,
        }
        if not all(report["cleanup"].values()):
            report["all_checks_passed"] = False
        report["seconds"] = round(time.perf_counter() - started, 6)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="swarm-detector:local")
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "docs" / "validation" / "container-results.json",
    )
    args = parser.parse_args(argv)
    try:
        report = smoke_image(args.image)
    except Exception as exc:
        report = {"all_checks_passed": False, "error": f"{type(exc).__name__}: {exc}"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
