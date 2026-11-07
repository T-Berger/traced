"""Active assessment identity comes from its exact run, even in a mixed case."""

from copy import deepcopy

import pytest

from evidence_explorer.review.workspace_views import context_header

MISSING = object()
VERSION = "saved-v3.2+original"


def text(node):
    if isinstance(node, str):
        return node
    if isinstance(node, (list, tuple)):
        return " ".join(text(child) for child in node)
    return text(getattr(node, "children", ""))


def mixed_view(method=MISSING):
    selected = {"id": "selected:run", "method_version": VERSION}
    if method is not MISSING:
        selected["method"] = method
    return {
        "bundle": {
            "case": {"id": "sources:mixed", "title": "Mixed saved case", "release_id": "release-7"},
            "case_type": "media",
            "swarm_context": {"baseline_run_id": "unrelated:run"},
            "records": [{"id": "media:record", "artifact_ref": "saved/frame.svg"}],
            "findings": [
                {
                    "snapshot_id": "unrelated:finding",
                    "run_id": "unrelated:run",
                    "details": {"domain": "swarm"},
                }
            ],
            "runs": [
                selected,
                {
                    "id": "unrelated:run",
                    "method": "swarm_detector.detector.analyze",
                    "method_version": "unrelated-version",
                },
            ],
        }
    }


def render_unchanged(view):
    original = deepcopy(view)
    rendered = context_header(view)
    assert view == original
    assert text(rendered[0]) == "Mixed saved case"
    assert "release-7" in text(rendered)
    return text(rendered[1]), text(rendered)


@pytest.mark.parametrize(
    ("method", "label"),
    [
        ("swarm_detector.detector.analyze", "Behavioral analysis"),
        ("supplied-media-metadata-and-byte-hashes", "Historical media"),
        ("saved_specialist_review_snapshot", "Historical media"),
        ("synthetic-specialist-review-handoff", "Historical media"),
        ("saved_source_collection", "Multi-platform sources"),
        ("evidence_explorer_repository_snapshot", "Agent investigation"),
        ("external_claim_check", "Agent investigation"),
        ("analyst_import", "Saved analysis"),
        ("synthetic_fixture", "Saved analysis"),
        ("authored_synthetic_comparison_example", "Saved analysis"),
    ],
)
def test_baseline_method_identity_ignores_other_specialists_and_preserves_exact_details(
    method, label
):
    heading, rendered = render_unchanged(mixed_view(method))
    assert heading == f"{label} · Baseline"
    assert f"Method: {method} · version {VERSION}" in rendered
    assert "Run: selected:run" in rendered
    assert "unrelated-version" not in rendered


@pytest.mark.parametrize("imported", [False, True], ids=["local-assessment", "imported-assessment"])
def test_selected_media_and_swarm_scenarios_override_case_and_baseline_context(imported):
    view = mixed_view("swarm_detector.detector.analyze")
    media = {
        "id": "media:run",
        "method": "supplied-media-metadata-and-byte-hashes",
        "method_version": "saved-media-v1",
    }
    view["bundle"]["runs"].append(media)
    view["baseline_scenario"] = {"id": "baseline", "base_run_id": "selected:run"}
    label = "Imported assessment 2" if imported else "Scenario 2"
    view["scenario"] = {
        "id": "selected:assessment",
        "base_run_id": "media:run",
        "version": 2,
        "label": label,
        "imported": imported,
    }
    view["is_baseline"] = False
    heading, rendered = render_unchanged(view)
    assert heading == f"Historical media · {label}"
    assert "Run: media:run" in rendered
    assert "Method: supplied-media-metadata-and-byte-hashes · version saved-media-v1" in rendered

    view["scenario"]["base_run_id"] = "selected:run"
    view["baseline_scenario"]["base_run_id"] = "media:run"
    heading, rendered = render_unchanged(view)
    assert heading == f"Behavioral analysis · {label}"
    assert f"Method: swarm_detector.detector.analyze · version {VERSION}" in rendered
    assert "Run: selected:run" in rendered


def test_saved_baseline_scenario_selects_its_exact_run_instead_of_first_run():
    view = mixed_view("swarm_detector.detector.analyze")
    view["bundle"]["runs"].append(
        {
            "id": "media:baseline",
            "method": "saved_specialist_review_snapshot",
            "method_version": "1",
        }
    )
    view["baseline_scenario"] = {
        "id": "saved:baseline",
        "base_run_id": "media:baseline",
        "version": 4,
        "imported": True,
    }
    view["is_baseline"] = True
    heading, rendered = render_unchanged(view)
    assert heading == "Historical media · Baseline"
    assert "Run: media:baseline" in rendered
    assert "Method: saved_specialist_review_snapshot · version 1" in rendered


@pytest.mark.parametrize(
    "method", [MISSING, None, "", " \t\n "], ids=["absent", "null", "empty", "whitespace"]
)
def test_missing_method_is_explicit_and_never_inferred_from_mixed_content(method):
    heading, rendered = render_unchanged(mixed_view(method))
    assert heading == "Method not supplied · Baseline"
    assert "Run: selected:run" in rendered
    assert f"version {VERSION}" in rendered


def test_unknown_method_with_specialist_words_does_not_gain_a_specialist_label():
    method = "swarm_detector.detector.analyze:derived-media-review"
    heading, rendered = render_unchanged(mixed_view(method))
    assert heading == "Saved analysis · Baseline"
    assert f"Method: {method} · version {VERSION}" in rendered


@pytest.mark.parametrize("missing", ["selected-reference", "all-runs"])
def test_missing_analysis_run_is_unavailable_without_falling_back_to_other_content(missing):
    view = mixed_view("swarm_detector.detector.analyze")
    if missing == "selected-reference":
        view["scenario"] = {
            "id": "missing:assessment",
            "base_run_id": "missing:run",
            "label": "Saved selection",
        }
        expected_label, expected_run = "Saved selection", "missing:run"
    else:
        view["bundle"]["runs"] = []
        expected_label, expected_run = "Baseline", "Not supplied"
    heading, rendered = render_unchanged(view)
    assert heading == f"Analysis run unavailable · {expected_label}"
    assert f"Run: {expected_run}" in rendered
    assert "Method: Not supplied · version Not supplied" in rendered
