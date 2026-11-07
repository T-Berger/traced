"""Check the actual integrated recipient's media callback and immutable artifact route.

All native workspaces are disposable. Real selected media is fetched only by a
Flask test client and never displayed. An optional loopback server contains only
an explicitly synthetic package for a separate, harmless browser check.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import inspect
import json
import signal
import sqlite3
import struct
import tempfile
import zlib
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from evidence_explorer.config import Settings
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.integrations.web import create_app
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from flask import request
from werkzeug.serving import make_server

from silent_storm.briefs import BriefService
from silent_storm.ingest import DETECTION_FIELDS, FRAME_FIELDS, import_release
from silent_storm.media import hash_media
from silent_storm.reviews import ReviewService
from silent_storm.shared_brief import SharedBriefExporter
from silent_storm.storage import Store, utc_now

PROJECT = Path(__file__).resolve().parents[1]
TABLES = ("review_scenarios", "review_events", "review_decisions", "review_jobs", "collection_jobs")


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def gray_png() -> bytes:
    """One uniformly gray pixel, generated with the standard library only."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x80\x80\x80"))
        + chunk(b"IEND", b"")
    )


def build_gray_package(root: Path) -> bytes:
    """Exercise the real specialist adapter with three manually attributed revisions."""
    source = root / "producer-release"
    (source / "metadata").mkdir(parents=True)
    (source / "frames").mkdir()
    (source / "frames/gray.png").write_bytes(gray_png())
    row = dict.fromkeys(FRAME_FIELDS, "")
    row.update(
        frame_id="gray-frame",
        file="gray.png",
        clip_id="synthetic-clip",
        video_id="synthetic-video",
        week="Week 11",
        side="Other",
        image_type="Photo",
        camera="RGB",
        width_px="1",
        height_px="1",
    )
    for filename, fields, rows in (
        ("frames.csv", FRAME_FIELDS, [row]),
        ("detections.csv", DETECTION_FIELDS, []),
    ):
        with (source / "metadata" / filename).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    store = Store(root / "producer.sqlite")
    import_release(
        store, source, release_id="synthetic-gray", title="Harmless synthetic gray pixel"
    )
    hash_media(store, "synthetic-gray")
    reviews = ReviewService(store)
    baseline = reviews.ensure_baseline("synthetic-gray")
    fields = dict(
        title="Synthetic gray fixture",
        observation="A uniform gray test pixel.",
        rationale="Explicitly synthetic technical fixture; no event is inferred.",
        evidence_ids=["synthetic-gray:gray-frame"],
        reviewer="Fixture author A",
        status="draft",
    )
    note = reviews.save_review(baseline["scenario_id"], **fields)
    for actor in ("Fixture author B", "Fixture author C"):
        fields.update(reviewer=actor, status="reviewed")
        note = reviews.save_review(
            baseline["scenario_id"],
            **fields,
            review_id=note["review_id"],
            expected_revision=note["revision"],
        )
    briefs = BriefService(store, reviews)
    preview = briefs.preview(baseline["scenario_id"], [note["review_id"]])
    saved = briefs.create(
        baseline["scenario_id"],
        title="Harmless synthetic recipient media fixture",
        reviewer="Automated recipient verification",
        source_origin="synthetic",
        review_versions=preview["review_versions"],
        expected_version=preview["expected_version"],
    )
    return SharedBriefExporter(store, briefs).package(saved["brief_id"])


def new_recipient(payload: bytes, root: Path):
    settings = Settings(data_dir=root / "workspace")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, root / "artifacts")
    case_id = service.import_package(payload)
    application = create_app(service)
    application.server.config["TESTING"] = True
    application.server.config["RECIPIENT_MEDIA_HTTP_CALLS"] = []

    @application.server.before_request
    def count_media_http():
        if request.path == "/trail-artifact":
            application.server.config["RECIPIENT_MEDIA_HTTP_CALLS"].append(request.full_path)

    return service, application, case_id


def _walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_text(child) for child in value)
    if isinstance(value, dict):
        return _text(value.get("props", {}).get("children"))
    return ""


def _images(value, *, closed_details: bool = False):
    if isinstance(value, dict):
        props = value.get("props", {})
        if value.get("type") == "Details":
            closed_details = closed_details or props.get("open") is not True
        if value.get("type") == "Img" and props.get("src"):
            yield {
                "src": props["src"],
                "inside_closed_details": closed_details,
                "loading": props.get("loading"),
                "alt": props.get("alt"),
                "display": props.get("style", {}).get("display"),
                "context": props.get("data-media-context"),
                "attempt": props.get("data-media-attempt"),
            }
        for child in value.values():
            yield from _images(child, closed_details=closed_details)
    elif isinstance(value, list):
        for child in value:
            yield from _images(child, closed_details=closed_details)


def media_callback(
    application,
    client,
    case_id: str,
    record_id: str,
    *,
    stored=None,
    action=None,
    event=None,
):
    """Invoke MEDIA-01's real multi-output callback, including pattern actions/events."""
    candidates = []
    for key, metadata in application.callback_map.items():
        outputs = metadata["output"]
        outputs = outputs if isinstance(outputs, list) else [outputs]
        if any(output.component_id == "trail-media" for output in outputs) and any(
            item["id"] == "rv-session" for item in metadata["inputs"]
        ):
            candidates.append((key, metadata, outputs))
    _assert(len(candidates) == 1, "The full recipient media transition callback is not registered.")
    key, metadata, outputs = candidates[0]
    specs = [
        {"id": output.component_id, "property": output.component_property} for output in outputs
    ]
    _assert(
        specs
        == [
            {"id": "trail-media", "property": "children"},
            {"id": "trail-media-view", "property": "data"},
        ],
        "The recipient media callback does not expose the released display-only outputs.",
    )
    trigger = "trail-media-event.data" if event is not None else "rv-session.data"
    if action:
        trigger = (
            json.dumps(
                {"type": "trail-media-action", "context": stored["context"], "action": action},
                sort_keys=True,
                separators=(",", ":"),
            )
            + ".n_clicks"
        )
    inputs = [
        {
            **item,
            "value": {"case_id": case_id, "record_id": record_id}
            if item["id"] == "rv-session"
            else [1]
            if item["property"] == "n_clicks" and action
            else []
            if item["property"] == "n_clicks"
            else event,
        }
        for item in metadata["inputs"]
    ]
    response = client.post(
        "/_dash-update-component",
        json={
            "output": key,
            "outputs": specs,
            "inputs": inputs,
            "state": [{"id": "trail-media-view", "property": "data", "value": stored}],
            "changedPropIds": [trigger],
        },
    )
    _assert(response.status_code == 200, "The real recipient media callback failed.")
    result = response.json["response"]
    return result["trail-media"]["children"], result["trail-media-view"]["data"]


def initial_readiness_callbacks(service, application, client, case_id: str) -> list[dict]:
    """Exercise the two real read-only startup projections, including lazy imports."""
    values = {
        "rv-session": session_for_case(service, case_id),
        "rv-draft-store": {},
        "rv-review-reason": "",
        "trail-export-attempt": None,
        "trail-use-case-purpose": {},
    }
    checks = []
    for target in ("trail-package", "trail-handoff-summary"):
        candidates = []
        for key, metadata in application.callback_map.items():
            outputs = (
                metadata["output"] if isinstance(metadata["output"], list) else [metadata["output"]]
            )
            if any(output.component_id == target for output in outputs) and any(
                item["id"] == "rv-session" for item in metadata["inputs"]
            ):
                candidates.append((key, metadata, outputs))
        _assert(len(candidates) == 1, f"Initial recipient callback {target} is not unambiguous.")
        key, metadata, outputs = candidates[0]
        specs = [
            {"id": output.component_id, "property": output.component_property} for output in outputs
        ]
        response = client.post(
            "/_dash-update-component",
            json={
                "output": key,
                "outputs": specs if isinstance(metadata["output"], list) else specs[0],
                "inputs": [
                    {**item, "value": values.get(item["id"])} for item in metadata["inputs"]
                ],
                "state": [{**item, "value": values.get(item["id"])} for item in metadata["state"]],
                "changedPropIds": ["rv-session.data"],
            },
        )
        _assert(response.status_code == 200, f"Initial recipient callback {target} failed.")
        checks.append(
            {
                "target": target,
                "http_status": response.status_code,
                "returned_fields": list(response.json["response"]),
            }
        )
    return checks


def _counts(database: Path) -> dict[str, int]:
    with sqlite3.connect(database) as connection:
        return {
            name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in TABLES
        }


def _no_media(body) -> bool:
    return not any(
        isinstance(node, dict) and node.get("type") == "Img" for node in _walk(body)
    ) and "/trail-artifact" not in json.dumps(body)


def _actions(body) -> set[str]:
    return {
        node["props"]["id"]["action"]
        for node in _walk(body)
        if isinstance(node, dict)
        and node.get("type") == "Button"
        and isinstance(node.get("props", {}).get("id"), dict)
        and node["props"]["id"].get("type") == "trail-media-action"
    }


def verify_package(payload: bytes, root: Path, *, synthetic: bool):
    """Exercise current MEDIA-01 transport in a disposable, read-only recipient."""
    from evidence_explorer.review.integrations.media_ui import resolve_media_selection

    service, application, case_id = new_recipient(payload, root)
    bundle = service.get_case(case_id)
    _assert(
        bundle["case"]["synthetic"] is synthetic,
        "Package origin does not match the explicit acceptance scope.",
    )
    records = [record for record in bundle["records"] if record.get("artifact_ref")]
    _assert(
        1 <= len(records) <= 12,
        "Recipient verification requires a bounded nonempty artifact selection.",
    )
    before = _counts(service.settings.db_path)
    fingerprint_before = service.repository.get_bundle(case_id).fingerprint()
    client = application.server.test_client()
    layout = client.get("/_dash-layout")
    _assert(layout.status_code == 200, "Integrated recipient layout is unavailable.")
    _assert(
        any(
            isinstance(node, dict) and node.get("props", {}).get("id") == "trail-media"
            for node in _walk(layout.json)
        ),
        "rv-media-slot was not populated with trail-media.",
    )
    stores = {
        node.get("props", {}).get("id"): node.get("props", {}).get("storage_type")
        for node in _walk(layout.json)
        if isinstance(node, dict) and node.get("type") == "Store"
    }
    _assert(
        all(stores.get(key) == "memory" for key in ("trail-media-view", "trail-media-event")),
        "The released media display/event stores must be memory-only.",
    )
    readiness = initial_readiness_callbacks(service, application, client, case_id)
    record = records[0]
    state = {"case_id": case_id, "record_id": record["id"]}
    http_calls = application.server.config["RECIPIENT_MEDIA_HTTP_CALLS"]
    original_artifact = service.artifact
    checked_paths = {}

    def observed_artifact(case, identifier, **kwargs):
        path = original_artifact(case, identifier, **kwargs)
        checked_paths[identifier] = path
        return path

    def step(*, stored=None, action=None, event=None):
        return media_callback(
            application, client, case_id, record["id"], stored=stored, action=action, event=event
        )

    def image_event(stored, status):
        return step(
            stored=stored,
            event={
                "context": stored["context"],
                "attempt": stored["attempt"],
                "status": status,
                "sequence": 1,
            },
        )

    def fetch_exact(url, expected_record):
        response = client.get(url)
        digest = hashlib.sha256(response.data).hexdigest()
        _assert(
            response.status_code == 200 and digest == expected_record["content_hash"],
            "Artifact HTTP bytes do not match the immutable native record content_hash.",
        )
        return {
            "record_id": expected_record["id"],
            "url": url,
            "http_status": response.status_code,
            "bytes": len(response.data),
            "expected_sha256": expected_record["content_hash"],
            "actual_sha256": digest,
            "content_type": response.mimetype,
            "nosniff": response.headers.get("X-Content-Type-Options"),
        }

    with patch.object(service, "artifact", side_effect=observed_artifact) as artifact_spy:
        initial_http = len(http_calls)
        hidden_body, hidden = step()
        selection = resolve_media_selection(service, state)
        _assert(
            selection["expected_hash"] == record["content_hash"]
            and selection["media_record_id"] == record["id"],
            "The selected media hash/identity changed.",
        )
        _assert(
            hidden.get("status") == "hidden" and _no_media(hidden_body),
            "Initial selection must render zero images and zero artifact src URLs.",
        )
        _assert(
            artifact_spy.call_count == 0 and len(http_calls) == initial_http,
            "Initial media selection must not preflight or request an artifact.",
        )
        initial = {
            "status": hidden["status"],
            "image_elements": 0,
            "media_src_attributes": 0,
            "artifact_preflight_calls": 0,
            "artifact_http_requests": 0,
            "expected_hash": selection["expected_hash"],
        }
        metadata = record.get("metadata", {})
        actual_fields = {}
        for node in _walk(hidden_body):
            if isinstance(node, dict) and node.get("type") == "Dl":
                children = node.get("props", {}).get("children") or []
                for index in range(0, len(children) - 1, 2):
                    actual_fields[_text(children[index])] = _text(children[index + 1])
        expected_fields = {
            "First-posted exercise week": str(
                metadata.get("week") or metadata.get("first_posted_exercise_week") or "Unknown"
            ),
            "Posting-channel affiliation": metadata.get("side")
            or metadata.get("posting_channel_affiliation")
            or "Unknown",
            "Clip / source video": f"{metadata.get('clip_id') or 'Unknown'} / {metadata.get('video_id') or 'Unknown'}",
            "Capture or event time": "Unknown",
            "Depicted side / location": "Unknown",
        }
        mismatches = {
            key: {"expected": value, "actual": actual_fields.get(key)}
            for key, value in expected_fields.items()
            if actual_fields.get(key) != value
        }
        _assert(
            not mismatches,
            "Recipient media labels differ from supplied metadata: " + json.dumps(mismatches),
        )

        loading_body, loading = step(stored=hidden, action="reveal")
        _assert(
            loading.get("status") == "loading" and artifact_spy.call_count == 1,
            "Explicit Reveal must make exactly one guarded file preflight.",
        )
        _assert(
            len(http_calls) == initial_http,
            "The callback transport itself must not perform an artifact HTTP GET.",
        )
        images = list(_images(loading_body))
        _assert(
            len(images) == 1 and images[0]["display"] == "none",
            "The first requested image must remain visually loading until its bound event.",
        )
        loading_image = images[0]
        params = parse_qs(urlsplit(loading_image["src"]).query)
        _assert(
            urlsplit(loading_image["src"]).path == "/trail-artifact"
            and params.get("case_id") == [case_id]
            and params.get("record_id") == [record["id"]]
            and set(params) <= {"case_id", "record_id", "attempt", "expected_hash"}
            and ("attempt" not in params or params["attempt"] == [loading["attempt"]])
            and (
                "expected_hash" not in params or params["expected_hash"] == [record["content_hash"]]
            ),
            "Reveal did not request the same immutable record/hash with a fresh bound attempt.",
        )
        _assert(
            loading_image["context"] == loading["context"]
            and loading_image["attempt"] == loading["attempt"],
            "Image events are not bound to the currently requested context and attempt.",
        )
        artifact_checks = [fetch_exact(loading_image["src"], record)]
        ready_body, ready = image_event(loading, "loaded")
        image = list(_images(ready_body))[0]
        _assert(
            ready["status"] == "ready"
            and image["display"] != "none"
            and image["src"] == loading_image["src"],
            "A matching load event did not complete the same image attempt.",
        )
        before_hide = artifact_spy.call_count
        hidden_body, hidden = step(stored=ready, action="hide")
        _assert(
            hidden["status"] == "hidden"
            and _no_media(hidden_body)
            and artifact_spy.call_count == before_hide,
            "Hide must remove image/src without another preflight.",
        )
        for artifact_record in records[1:]:
            artifact_checks.append(
                fetch_exact(
                    "/trail-artifact?"
                    + urlencode({"case_id": case_id, "record_id": artifact_record["id"]}),
                    artifact_record,
                )
            )

        path = checked_paths[record["id"]]
        original = path.read_bytes()
        negative = {}
        for failure in ("changed", "missing"):
            if failure == "changed":
                path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
            else:
                path.unlink()
            old_calls, old_http = artifact_spy.call_count, len(http_calls)
            failed_body, failed = step(stored=hidden, action="reveal")
            code = "changed" if failure == "changed" else "unavailable"
            _assert(
                failed.get("status") == "error"
                and failed.get("error") == code
                and _no_media(failed_body),
                "Changed/missing media needs an inline error with the image and src removed.",
            )
            _assert(
                {"retry", "hide"} <= _actions(failed_body)
                and "Inspect saved metadata" in _text(failed_body),
                "Inline recovery must keep Retry, Hide and saved-metadata inspection available.",
            )
            _assert(
                artifact_spy.call_count == old_calls + 1 and len(http_calls) == old_http,
                "Failed preflight must not create a media GET or an unguarded image URL.",
            )
            _assert(str(path) not in _text(failed_body), "Recovery exposed an absolute local path.")
            rejected = client.get(image["src"])
            _assert(
                rejected.status_code == 404,
                "The independent artifact route served changed/missing bytes.",
            )
            path.write_bytes(original)
            retry_body, retry = step(stored=failed, action="retry")
            retry_image = list(_images(retry_body))[0]
            _assert(
                retry["status"] == "loading" and retry["attempt"] != failed["attempt"],
                "Retry must guard the same saved hash with a fresh attempt.",
            )
            retry_check = fetch_exact(retry_image["src"], record)
            _, ready = image_event(retry, "loaded")
            hidden_body, hidden = step(stored=ready, action="hide")
            negative[failure] = {
                "inline_error": code,
                "image_removed": True,
                "src_removed": True,
                "artifact_preflight_calls": 1,
                "automatic_artifact_http_requests": 0,
                "guarded_http_status": rejected.status_code,
                "retry_http_status": retry_check["http_status"],
                "retry_sha256": retry_check["actual_sha256"],
                "fresh_attempt": True,
            }

        # Exercise the real HTTP404 -> browser-error event -> retry state machine.
        loading_body, loading = step(stored=hidden, action="reveal")
        path.unlink()
        failed_url = list(_images(loading_body))[0]["src"]
        postflight = client.get(failed_url)
        _assert(postflight.status_code == 404, "A file removed after preflight was still served.")
        display_body, display_error = image_event(loading, "error")
        _assert(
            display_error.get("error") == "display" and _no_media(display_body),
            "A bound image-load error must remove the failed image/src and offer recovery.",
        )
        path.write_bytes(original)
        retry_body, retry = step(stored=display_error, action="retry")
        retry_check = fetch_exact(list(_images(retry_body))[0]["src"], record)
        _, ready = image_event(retry, "loaded")
        hidden_body, hidden = step(stored=ready, action="hide")
        _assert(_no_media(hidden_body), "The final hidden view retained an image source.")
        negative["post_preflight_missing"] = {
            "guarded_http_status": postflight.status_code,
            "inline_error": display_error["error"],
            "image_removed": True,
            "retry_http_status": retry_check["http_status"],
            "retry_sha256": retry_check["actual_sha256"],
            "fresh_attempt": retry["attempt"] != loading["attempt"],
        }
        empty_body, empty_store = media_callback(application, client, case_id, "", stored=hidden)
        _assert(
            _no_media(empty_body) and not empty_store,
            "An empty selection retained old media/consent.",
        )

    _assert(
        service.repository.get_bundle(case_id).fingerprint() == fingerprint_before,
        "Recipient evidence changed during media display/recovery checks.",
    )
    after = _counts(service.settings.db_path)
    _assert(
        before == after and all(value == 0 for value in after.values()),
        "Recipient checks created decisions, scenarios, events, jobs or collection work.",
    )
    result = {
        "case_id": case_id,
        "synthetic": synthetic,
        "records": len(bundle["records"]),
        "findings": len(bundle["findings"]),
        "shared_reviews": len(bundle["reviews"]),
        "package_sha256": hashlib.sha256(payload).hexdigest(),
        "package_bytes": len(payload),
        "workspace_root": str(root),
        "workspace_db": str(service.settings.db_path),
        "artifact_root": str(service.artifact_root),
        "native_integrated_layout": True,
        "media_public_api": "MEDIA-01 transition_media multi-output callback",
        "memory_stores": {key: stores[key] for key in ("trail-media-view", "trail-media-event")},
        "nonempty_trail_media_callback": True,
        "initial_readiness_callbacks": readiness,
        "initial_hidden": initial,
        "explicit_reveal": {
            "preflight_calls": 1,
            "expected_hash": selection["expected_hash"],
            "media_record_id": record["id"],
            "attempt": loading_image["attempt"],
            "guarded_artifact_http_status": artifact_checks[0]["http_status"],
        },
        "image_after_explicit_load_event": image,
        "real_graphic_display_performed": False,
        "real_artifact_http_fetched_without_display": not synthetic,
        "browser_run": False,
        "image_load_error_events": "Documented bound events supplied through actual callback HTTP; no browser image display occurred.",
        "metadata_fields": actual_fields,
        "metadata_field_mismatches": mismatches,
        "adapter_label_keys": {
            key: key in metadata
            for key in ("first_posted_exercise_week", "posting_channel_affiliation")
        },
        "artifact_http_checks": artifact_checks,
        "negative_checks": negative,
        "final_hidden": {
            "status": hidden["status"],
            "image_elements": 0,
            "media_src_attributes": 0,
        },
        "empty_selection_has_no_media": True,
        "immutable_bundle_unchanged": True,
        "before_table_counts": before,
        "after_table_counts": after,
    }
    return service, application, result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--actual-package", type=Path, default=PROJECT / "artifacts/selected-case.zip"
    )
    parser.add_argument("--synthetic-package", type=Path)
    parser.add_argument(
        "--report",
        type=Path,
        default=PROJECT / "artifacts/recipient-media-after-viewer-verification.json",
    )
    parser.add_argument(
        "--serve",
        type=int,
        metavar="PORT",
        help="Serve only the harmless synthetic recipient on loopback.",
    )
    args = parser.parse_args(argv)
    report = {
        "schema_version": "silent-storm-recipient-media/2",
        "status": "running",
        "started_at": utc_now(),
        "native_app": "evidence_explorer.review.integrations.web.create_app",
        "native_app_file": inspect.getfile(create_app),
        "native_app_source_sha256": hashlib.sha256(
            Path(inspect.getfile(create_app)).read_bytes()
        ).hexdigest(),
        "graphic_display_performed": False,
        "models_run": 0,
        "workers_started": 0,
        "demo_seeds_run": 0,
        "operational_workspace_writes": False,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)

    def write_report():
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        )

    try:
        with tempfile.TemporaryDirectory(prefix="traced-recipient-media-") as directory:
            scratch = Path(directory)
            synthetic_payload = (
                args.synthetic_package.read_bytes()
                if args.synthetic_package
                else build_gray_package(scratch)
            )
            _, synthetic_app, report["synthetic"] = verify_package(
                synthetic_payload, scratch / "synthetic-recipient", synthetic=True
            )
            _, _, report["actual"] = verify_package(
                args.actual_package.read_bytes(), scratch / "actual-recipient", synthetic=False
            )
            report["actual_package"] = str(args.actual_package.resolve())
            report["synthetic_package"] = (
                str(args.synthetic_package.resolve())
                if args.synthetic_package
                else "generated harmless gray pixel with three specialist revisions"
            )
            report["status"] = "passed"
            if args.serve:
                synthetic_app.server.config["TESTING"] = False
                server = make_server("127.0.0.1", args.serve, synthetic_app.server)
                report["server"] = {
                    "url": f"http://127.0.0.1:{args.serve}/",
                    "port": args.serve,
                    "synthetic_only": True,
                    "running": True,
                }
                write_report()
                print(
                    json.dumps(
                        {
                            "status": "ready",
                            "url": report["server"]["url"],
                            "case_id": report["synthetic"]["case_id"],
                            "report": str(args.report),
                        }
                    ),
                    flush=True,
                )

                def stop(_signal, _frame):
                    raise KeyboardInterrupt

                signal.signal(signal.SIGTERM, stop)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    server.server_close()
                    report["server"].update(running=False, stopped_at=utc_now())
            report["completed_at"] = utc_now()
        report["temporary_workspaces_removed"] = True
        write_report()
        print(json.dumps({"status": report["status"], "report": str(args.report)}), flush=True)
        return 0
    except Exception as error:
        report.update(
            status="failed",
            error={"type": type(error).__name__, "message": str(error)},
            completed_at=utc_now(),
        )
        write_report()
        print(
            json.dumps({"status": "failed", "report": str(args.report), "error": report["error"]}),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
