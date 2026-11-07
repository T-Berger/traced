"""The integrated native recipient must resolve real media, not an empty base slot."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/verify_recipient_media.py"
SPEC = importlib.util.spec_from_file_location("recipient_media_acceptance", SCRIPT)
recipient = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recipient)


@pytest.fixture
def harmless_package(tmp_path):
    return recipient.build_gray_package(tmp_path / "producer")


def test_integrated_recipient_reveals_gray_media_explicitly_and_recovers_exact_bytes(
    harmless_package,
    tmp_path,
):
    _, _, result = recipient.verify_package(
        harmless_package, tmp_path / "recipient", synthetic=True
    )
    assert result["native_integrated_layout"] and result["nonempty_trail_media_callback"]
    assert [item["http_status"] for item in result["initial_readiness_callbacks"]] == [200, 200]
    assert result["memory_stores"] == {"trail-media-view": "memory", "trail-media-event": "memory"}
    assert result["initial_hidden"]["status"] == "hidden"
    assert result["initial_hidden"]["image_elements"] == 0
    assert result["initial_hidden"]["media_src_attributes"] == 0
    assert result["initial_hidden"]["artifact_preflight_calls"] == 0
    assert result["initial_hidden"]["artifact_http_requests"] == 0
    assert result["explicit_reveal"]["preflight_calls"] == 1
    assert result["image_after_explicit_load_event"]["display"] != "none"
    assert result["metadata_fields"]["First-posted exercise week"] == "11"
    assert result["metadata_fields"]["Posting-channel affiliation"] == "Other"
    assert result["metadata_field_mismatches"] == {}
    assert (
        result["artifact_http_checks"][0]["actual_sha256"]
        == result["artifact_http_checks"][0]["expected_sha256"]
    )
    expected_hash = result["artifact_http_checks"][0]["expected_sha256"]
    for failure, error in (("changed", "changed"), ("missing", "unavailable")):
        recovery = result["negative_checks"][failure]
        assert recovery["inline_error"] == error
        assert recovery["image_removed"] and recovery["src_removed"]
        assert recovery["automatic_artifact_http_requests"] == 0
        assert recovery["guarded_http_status"] == 404
        assert recovery["retry_http_status"] == 200
        assert recovery["retry_sha256"] == expected_hash
        assert recovery["fresh_attempt"]
    postflight = result["negative_checks"]["post_preflight_missing"]
    assert postflight["guarded_http_status"] == 404
    assert postflight["inline_error"] == "display"
    assert postflight["image_removed"]
    assert postflight["retry_sha256"] == expected_hash
    assert postflight["fresh_attempt"]
    assert result["final_hidden"]["image_elements"] == 0
    assert result["final_hidden"]["media_src_attributes"] == 0
    assert result["before_table_counts"] == result["after_table_counts"]
    assert all(value == 0 for value in result["after_table_counts"].values())


def test_empty_selection_resets_display_consent_without_preflight_or_image(
    harmless_package, tmp_path
):
    service, app, case_id = recipient.new_recipient(harmless_package, tmp_path / "recipient")
    client = app.server.test_client()
    record = next(
        record for record in service.get_case(case_id)["records"] if record.get("artifact_ref")
    )
    with recipient.patch.object(service, "artifact", wraps=service.artifact) as preflight:
        hidden_body, hidden = recipient.media_callback(app, client, case_id, record["id"])
        assert hidden["status"] == "hidden" and recipient._no_media(hidden_body)
        body, store = recipient.media_callback(app, client, case_id, "", stored=hidden)
        assert recipient._no_media(body) and store == {}
        assert preflight.call_count == 0
    assert app.server.config["RECIPIENT_MEDIA_HTTP_CALLS"] == []
    layout = client.get("/_dash-layout").json
    assert any(
        isinstance(node, dict) and node.get("props", {}).get("id") == "trail-media"
        for node in recipient._walk(layout)
    )


def test_recipient_metadata_action_targets_the_exact_saved_csv_record(harmless_package, tmp_path):
    service, app, case_id = recipient.new_recipient(harmless_package, tmp_path / "recipient")
    client = app.server.test_client()
    records = service.get_case(case_id)["records"]
    image = next(record for record in records if record.get("artifact_ref"))
    metadata = next(record for record in records if record["kind"] == "supplied_frame_metadata")
    service.artifact(case_id, image["id"]).unlink()

    with recipient.patch.object(service, "artifact", wraps=service.artifact) as preflight:
        _, hidden = recipient.media_callback(app, client, case_id, image["id"])
        body, error = recipient.media_callback(
            app, client, case_id, image["id"], stored=hidden, action="reveal"
        )
        assert error["error"] == "unavailable"
        button = next(
            node["props"]
            for node in recipient._walk(body)
            if isinstance(node, dict)
            and node.get("type") == "Button"
            and node["props"].get("children") == "Inspect saved metadata"
        )
        assert button["id"]["rid"] == metadata["id"]
        metadata_body, metadata_view = recipient.media_callback(
            app, client, case_id, button["id"]["rid"], stored=error
        )
        assert metadata_view["status"] == "hidden"
        assert metadata_view["context"] != hidden["context"]
        assert recipient._no_media(metadata_body)
        assert preflight.call_count == 1


def test_real_package_starts_hidden_and_is_http_verified_without_graphic_display(tmp_path):
    package = Path(__file__).resolve().parents[2] / "artifacts/selected-case.zip"
    if not package.is_file():
        pytest.skip(
            "Optional generated selected-case package is absent; synthetic recipient checks remain mandatory."
        )
    _, _, result = recipient.verify_package(
        package.read_bytes(), tmp_path / "real-recipient", synthetic=False
    )
    assert result["initial_hidden"]["image_elements"] == 0
    assert result["initial_hidden"]["media_src_attributes"] == 0
    assert result["initial_hidden"]["artifact_preflight_calls"] == 0
    assert result["initial_hidden"]["artifact_http_requests"] == 0
    assert result["explicit_reveal"]["guarded_artifact_http_status"] == 200
    assert (
        result["explicit_reveal"]["expected_hash"]
        == result["artifact_http_checks"][0]["actual_sha256"]
    )
    assert result["real_artifact_http_fetched_without_display"] is True
    assert result["real_graphic_display_performed"] is False
    assert result["browser_run"] is False
    assert result["shared_reviews"] == 0
