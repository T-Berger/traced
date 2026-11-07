"""Activity cues require comparable evidence and explicit analyst configuration.

The photographed 20 percent value is an illustration, not a validated default.
These tests exercise mathematical and provenance boundaries, without registering
monitors, running a worker, or interpreting activity as truth or hostile intent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from evidence_explorer.use_cases.alerts import (
    evaluate_activity_alert,
    illustrative_activity_example,
)
from evidence_explorer.use_cases.contracts import ActivityWindow, AlertRule, WindowDefinition

START = datetime(2026, 11, 6, 10, tzinfo=UTC)


def _rule(**changes):
    values = {
        "id": "review:explicit-rule",
        "title": "Explicit analyst-selected activity change",
        "metric": "supplied topic-labelled records",
        "threshold_percent": 20,
        "threshold_basis": "analyst_configured",
        "direction": "increase",
        "comparison_basis": "equal_duration_counts",
        "min_baseline_count": 5,
        "min_current_count": 5,
        "min_coverage_fraction": 1,
    }
    return AlertRule.model_validate(values | changes)


def _window(name, count, **changes):
    start = START if name == "baseline" else START + timedelta(hours=1)
    values = {
        "id": name,
        "start": start,
        "end": start + timedelta(hours=1),
        "metric": "supplied topic-labelled records",
        "source_ids": ("supplied:public-a", "supplied:public-b"),
        "observed_count": count,
        "record_ids": tuple(f"{name}:record:{index}" for index in range(count)),
        "timestamp_basis": "publication",
        "coverage_status": "complete",
        "coverage_fraction": 1,
        "collection_method": "explicit saved public sample",
        "synthetic": True,
    }
    return ActivityWindow.model_validate(values | changes)


def _changed(window, **changes):
    return ActivityWindow.model_validate(window.model_dump() | changes)


def test_rule_requires_explicit_threshold_counts_and_coverage_without_defaults():
    values = _rule().model_dump()
    for required in (
        "threshold_percent",
        "threshold_basis",
        "direction",
        "comparison_basis",
        "min_baseline_count",
        "min_current_count",
        "min_coverage_fraction",
    ):
        incomplete = {key: value for key, value in values.items() if key != required}
        with pytest.raises(ValidationError):
            AlertRule.model_validate(incomplete)
    rule, baseline, current = illustrative_activity_example()
    result = evaluate_activity_alert(rule, baseline, current)
    assert result.threshold_basis == "illustrative_unvalidated"
    assert any("unvalidated illustration" in text for text in result.limitations)


def test_exact_threshold_requests_only_review_and_preserves_the_cited_inputs():
    baseline, current = _window("baseline", 10), _window("current", 12)
    before = baseline.model_dump(), current.model_dump()
    result = evaluate_activity_alert(_rule(), baseline, current)
    assert result.status == "review_needed"
    assert result.relative_change_percent == pytest.approx(20)
    assert result.record_ids == tuple(sorted(baseline.record_ids + current.record_ids))
    assert result.origin == "synthetic"
    assert any("not a factual or intent verdict" in text for text in result.limitations)
    assert any("not a registered live alert or notification" in text for text in result.limitations)
    assert (baseline.model_dump(), current.model_dump()) == before
    assert evaluate_activity_alert(_rule(threshold_percent=21), baseline, current).status == (
        "not_triggered"
    )


def test_twice_as_many_records_in_twice_the_time_is_not_a_comparable_raw_count_spike():
    baseline = _window("baseline", 10)
    current = _window("current", 20, end=START + timedelta(hours=3))
    result = evaluate_activity_alert(_rule(), baseline, current)
    assert result.status == "unavailable"
    assert "equal-duration" in result.summary
    assert result.relative_change_percent is None


def test_explicit_exposure_normalization_compares_rates_instead_of_raw_counts():
    rule = _rule(comparison_basis="exposure_normalized")
    baseline = _window("baseline", 10, exposure=100, exposure_unit="sampled public records")
    current = _window(
        "current",
        20,
        exposure=200,
        exposure_unit="sampled public records",
        end=START + timedelta(hours=3),
    )
    result = evaluate_activity_alert(rule, baseline, current)
    assert result.status == "not_triggered"
    assert result.relative_change_percent == pytest.approx(0)
    assert result.baseline_value == result.observation_value == pytest.approx(0.1)
    # The raw count falls while the share of the explicitly supplied exposure rises.
    baseline = _window("baseline", 20, exposure=100, exposure_unit="sampled public records")
    current = _window("current", 15, exposure=50, exposure_unit="sampled public records")
    result = evaluate_activity_alert(rule, baseline, current)
    assert result.status == "review_needed"
    assert result.relative_change_percent == pytest.approx(50)
    for changes in ({"exposure": None}, {"exposure_unit": "another denominator"}):
        unavailable = evaluate_activity_alert(rule, baseline, _changed(current, **changes))
        assert unavailable.status == "unavailable"
        assert unavailable.relative_change_percent is None


def test_minimum_counts_and_declared_coverage_gate_an_otherwise_large_change():
    cases = (
        (_window("baseline", 3), _window("current", 30), "baseline count"),
        (_window("baseline", 10), _window("current", 1), "observation count"),
        (
            _window("baseline", 10),
            _window(
                "current",
                30,
                coverage_status="partial",
                coverage_fraction=0.8,
            ),
            "coverage",
        ),
    )
    for baseline, current, reason in cases:
        result = evaluate_activity_alert(_rule(), baseline, current)
        assert result.status == "unavailable", reason
        assert reason in result.summary
        assert result.relative_change_percent is None


def test_a_zero_baseline_is_unknown_relative_change_not_an_infinite_alert():
    result = evaluate_activity_alert(_rule(), _window("baseline", 0), _window("current", 10))
    assert result.status == "unavailable"
    assert "zero baseline" in result.summary
    assert result.relative_change_percent is None
    assert result.observation_value is None


def test_missing_windows_unknown_dates_or_time_assignments_cannot_produce_a_signal():
    baseline, current = _window("baseline", 10), _window("current", 30)
    assert evaluate_activity_alert(_rule(), None, current).status == "unavailable"
    for changes in (
        {"start": None},
        {"end": None},
        {"timestamp_basis": "unknown"},
        {"unknown_time_count": 1},
        {"coverage_status": "unknown", "coverage_fraction": None},
    ):
        result = evaluate_activity_alert(_rule(), baseline, _changed(current, **changes))
        assert result.status == "unavailable", changes
        assert result.relative_change_percent is None


def test_mismatched_definitions_sources_methods_and_overlapping_windows_are_unavailable():
    baseline, current = _window("baseline", 10), _window("current", 30)
    for changes in (
        {"metric": "another metric"},
        {"source_ids": ("another:source",)},
        {"collection_method": "different sampling method"},
        {"timestamp_basis": "acquisition"},
        {"start": START + timedelta(minutes=30), "end": START + timedelta(minutes=90)},
    ):
        result = evaluate_activity_alert(_rule(), baseline, _changed(current, **changes))
        assert result.status == "unavailable", changes
        assert result.relative_change_percent is None


def test_missing_duplicate_or_shared_record_references_cannot_inflate_activity():
    baseline, current = _window("baseline", 10), _window("current", 30)
    missing = evaluate_activity_alert(_rule(), baseline, _changed(current, record_ids=()))
    assert missing.status == "unavailable"
    assert "unique cited records" in missing.summary
    shared_refs = (baseline.record_ids[0],) + current.record_ids[1:]
    shared = evaluate_activity_alert(_rule(), baseline, _changed(current, record_ids=shared_refs))
    assert shared.status == "unavailable"
    assert "both windows" in shared.summary
    with pytest.raises(ValidationError):
        _changed(current, record_ids=("repeated:id",) * current.observed_count)


def test_nonfinite_or_boolean_measurements_are_rejected_instead_of_rendered_as_signals():
    for value in (float("nan"), float("inf"), float("-inf"), True):
        with pytest.raises(ValidationError):
            _rule(threshold_percent=value)
        with pytest.raises(ValidationError):
            _window("baseline", 10, exposure=value)
        with pytest.raises(ValidationError):
            _window("baseline", 10, coverage_fraction=value)
    for changes in ({"observed_count": True}, {"observed_count": 10.0}, {"exposure": 0}):
        with pytest.raises(ValidationError):
            _window("baseline", 10, **changes)


def test_decreases_are_review_candidates_only_when_the_explicit_direction_includes_them():
    baseline, current = _window("baseline", 10), _window("current", 8)
    assert evaluate_activity_alert(_rule(direction="increase"), baseline, current).status == (
        "not_triggered"
    )
    for direction in ("decrease", "either"):
        result = evaluate_activity_alert(_rule(direction=direction), baseline, current)
        assert result.status == "review_needed"
        assert result.relative_change_percent == pytest.approx(-20)


def test_equal_wall_clock_windows_across_dst_must_compare_actual_elapsed_time():
    zone = ZoneInfo("Europe/Berlin")
    baseline = _window(
        "baseline",
        10,
        start=datetime(2026, 10, 25, 1, tzinfo=zone),
        end=datetime(2026, 10, 25, 3, tzinfo=zone),
    )
    current = _window(
        "current",
        12,
        start=datetime(2026, 10, 25, 3, tzinfo=zone),
        end=datetime(2026, 10, 25, 5, tzinfo=zone),
    )
    result = evaluate_activity_alert(_rule(), baseline, current)
    assert result.status == "unavailable"  # Three elapsed hours versus two.
    assert "equal-duration" in result.summary
    repeated_hour = WindowDefinition(
        id="explicit-repeated-hour",
        start=datetime(2026, 10, 25, 2, tzinfo=zone, fold=0),
        end=datetime(2026, 10, 25, 2, tzinfo=zone, fold=1),
    )
    assert repeated_hour.end.timestamp() - repeated_hour.start.timestamp() == 3600
