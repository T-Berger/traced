"""Inclusive activity thresholds use declared decimal measurements exactly."""

from datetime import UTC, datetime, timedelta
from math import inf, nextafter

import pytest

from evidence_explorer.use_cases.alerts import evaluate_activity_alert
from evidence_explorer.use_cases.contracts import ActivityWindow, AlertRule


def _rule(*, direction="increase", threshold=20, comparison_basis="exposure_normalized"):
    return AlertRule(
        id="rule:precision",
        title="Explicit activity comparison",
        metric="saved records",
        threshold_percent=threshold,
        threshold_basis="analyst_configured",
        direction=direction,
        comparison_basis=comparison_basis,
        min_baseline_count=1,
        min_current_count=0,
        min_coverage_fraction=1,
    )


def _window(name, count, exposure):
    start = datetime(2026, 11, 7, tzinfo=UTC)
    if name == "observation":
        start += timedelta(hours=1)
    return ActivityWindow(
        id=name,
        start=start,
        end=start + timedelta(hours=1),
        metric="saved records",
        source_ids=("source:bounded-sample",),
        observed_count=count,
        record_ids=tuple(f"{name}:{index}" for index in range(count)),
        timestamp_basis="publication",
        coverage_status="complete",
        coverage_fraction=1,
        collection_method="saved bounded sample",
        exposure=exposure,
        exposure_unit="declared sample opportunities",
        synthetic=True,
    )


@pytest.mark.parametrize(
    "before,after,baseline_exposure,observation_exposure,direction,threshold,change",
    [
        (10, 12, 100, 100, "increase", 20, 20),
        (30, 27, 100, 100, "decrease", 10, -10),
        (10, 12, 100, 100, "either", 20, 20),
        (30, 27, 100, 100, "either", 10, -10),
        (10, 18, 100, 150, "increase", 20, 20),
        (1, 3, 0.1, 0.25, "increase", 20, 20),
        (32, 36, 100, 100, "increase", 12.5, 12.5),
    ],
)
def test_normalized_exact_threshold_is_inclusive(
    before, after, baseline_exposure, observation_exposure, direction, threshold, change
):
    baseline = _window("baseline", before, baseline_exposure)
    observation = _window("observation", after, observation_exposure)
    saved_inputs = baseline.model_dump(), observation.model_dump()

    result = evaluate_activity_alert(
        _rule(direction=direction, threshold=threshold), baseline, observation
    )

    assert result.status == "review_needed"
    assert result.relative_change_percent == change
    assert result.record_ids == tuple(sorted(baseline.record_ids + observation.record_ids))
    assert (baseline.model_dump(), observation.model_dump()) == saved_inputs


@pytest.mark.parametrize("comparison_basis", ["equal_duration_counts", "exposure_normalized"])
@pytest.mark.parametrize(
    "threshold,expected",
    [(nextafter(20, 0), "review_needed"), (nextafter(20, inf), "not_triggered")],
)
def test_adjacent_decimal_thresholds_are_not_swallowed_by_a_tolerance(
    comparison_basis, threshold, expected
):
    result = evaluate_activity_alert(
        _rule(threshold=threshold, comparison_basis=comparison_basis),
        _window("baseline", 10, 100),
        _window("observation", 12, 100),
    )

    assert result.status == expected
    assert result.relative_change_percent == 20


@pytest.mark.parametrize(
    "exposure,expected",
    [(nextafter(100, 0), "review_needed"), (nextafter(100, inf), "not_triggered")],
)
def test_rates_just_above_and_below_threshold_keep_their_distinct_decisions(exposure, expected):
    result = evaluate_activity_alert(
        _rule(), _window("baseline", 10, 100), _window("observation", 12, exposure)
    )

    assert result.status == expected


def test_finite_rates_do_not_overflow_an_intermediate_percentage_product():
    result = evaluate_activity_alert(
        _rule(threshold=100),
        _window("baseline", 10, 1e-306),
        _window("observation", 20, 1e-306),
    )

    assert result.status == "review_needed"
    assert result.baseline_value == 1e307
    assert result.observation_value == 2e307
    assert result.relative_change_percent == 100


@pytest.mark.parametrize(
    "baseline_exposure,observation_exposure,reason",
    [
        (1e-308, 1, "normalized values are non-finite"),
        (1, 1e-308, "normalized values are non-finite"),
        (1e308, 1e-306, "relative change is not finite"),
    ],
)
def test_unrepresentable_rates_or_change_remain_unavailable(
    baseline_exposure, observation_exposure, reason
):
    result = evaluate_activity_alert(
        _rule(),
        _window("baseline", 10, baseline_exposure),
        _window("observation", 12, observation_exposure),
    )

    assert result.status == "unavailable"
    assert reason in result.summary
    assert result.baseline_value is None
    assert result.observation_value is None
    assert result.relative_change_percent is None
