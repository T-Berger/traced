"""Transparent comparison of explicitly supplied, bounded activity windows.

This evaluator classifies no content and starts no live monitoring. Counts refer
to the cited records, while an exposure-normalized comparison uses the supplied
denominator and unit. Crossing a configured threshold asks a human to review.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from fractions import Fraction

from .contracts import ActivityWindow, AlertCoverage, AlertEvaluation, AlertRule


def evaluate_activity_alert(
    rule: AlertRule,
    baseline: ActivityWindow | None,
    current: ActivityWindow | None,
) -> AlertEvaluation:
    rule = AlertRule.model_validate(rule)
    baseline = ActivityWindow.model_validate(baseline) if baseline is not None else None
    current = ActivityWindow.model_validate(current) if current is not None else None
    windows = [window for window in (baseline, current) if window is not None]
    record_ids = tuple(sorted({rid for window in windows for rid in window.record_ids}))
    origins = {window.synthetic for window in windows}
    origin = (
        "synthetic" if origins == {True} else "mixed" if len(origins) > 1 else "supplied_unknown"
    )
    coverage = AlertCoverage(
        baseline_fraction=baseline.coverage_fraction if baseline else None,
        observation_fraction=current.coverage_fraction if current else None,
        source_ids=baseline.source_ids if baseline else (),
        timestamp_basis=baseline.timestamp_basis if baseline else "unknown",
        collection_method=baseline.collection_method if baseline else None,
        exposure_unit=baseline.exposure_unit if baseline else None,
    )
    limits = [
        "A threshold signal requires human review; it is not a factual or intent verdict.",
        "Metric labels are supplied by the caller; this evaluator infers no hate, bot, "
        "or narrative labels.",
        "Coverage describes the declared source subset, not the entire information space.",
        "This is a read-only evaluation, not a registered live alert or notification service.",
    ]
    if rule.threshold_basis == "illustrative_unvalidated":
        limits.append("The threshold is an unvalidated illustration from workshop notes.")
    method = (
        "Relative change = 100 × (observation value − baseline value) / baseline value. "
        "The configured threshold is inclusive. "
        + (
            "Values are record counts in equal-duration windows."
            if rule.comparison_basis == "equal_duration_counts"
            else "Values are record counts divided by supplied exposure in the same declared unit."
        )
    )

    def unavailable(reasons: list[str]) -> AlertEvaluation:
        return AlertEvaluation(
            rule_id=rule.id,
            status="unavailable",
            summary="Comparison unavailable: " + "; ".join(reasons) + ".",
            comparison_basis=rule.comparison_basis,
            threshold_percent=rule.threshold_percent,
            threshold_basis=rule.threshold_basis,
            record_ids=record_ids,
            method=method,
            coverage=coverage,
            origin=origin,
            limitations=tuple(limits),
        )

    if baseline is None or current is None:
        return unavailable(["both baseline and observation windows are required"])
    problems = []
    for name, window in (("baseline", baseline), ("observation", current)):
        if window.start is None or window.end is None:
            problems.append(f"{name} window boundaries are unknown")
        if window.timestamp_basis == "unknown" or window.unknown_time_count:
            problems.append(f"{name} contains unknown time assignments")
        if window.coverage_status == "unknown" or window.coverage_fraction is None:
            problems.append(f"{name} coverage is unknown")
        elif window.coverage_fraction < rule.min_coverage_fraction:
            problems.append(f"{name} coverage is below the configured minimum")
        if window.observed_count is None:
            problems.append(f"{name} count is unknown")
        elif window.observed_count != len(window.record_ids):
            problems.append(f"{name} count does not match its unique cited records")
    if baseline.metric != rule.metric or current.metric != rule.metric:
        problems.append("metric definitions do not match the rule")
    if set(baseline.source_ids) != set(current.source_ids):
        problems.append("source selections differ")
    if baseline.collection_method != current.collection_method:
        problems.append("collection methods differ")
    if baseline.timestamp_basis != current.timestamp_basis:
        problems.append("time bases differ")
    if set(baseline.record_ids) & set(current.record_ids):
        problems.append("a supplied record is counted in both windows")
    if (
        current.start is not None
        and baseline.end is not None
        and current.start.astimezone(UTC) < baseline.end.astimezone(UTC)
    ):
        problems.append("the observation window overlaps or precedes the baseline")
    if baseline.observed_count is not None and baseline.observed_count < rule.min_baseline_count:
        problems.append("baseline count is below the configured minimum")
    if baseline.observed_count == 0:
        problems.append("a relative percentage is undefined for a zero baseline")
    if current.observed_count is not None and current.observed_count < rule.min_current_count:
        problems.append("observation count is below the configured minimum")
    if rule.comparison_basis == "equal_duration_counts":
        if all(
            window.start is not None and window.end is not None for window in (baseline, current)
        ):
            if baseline.end.astimezone(UTC) - baseline.start.astimezone(
                UTC
            ) != current.end.astimezone(UTC) - current.start.astimezone(UTC):
                problems.append("count comparison requires equal-duration windows")
    else:
        if baseline.exposure is None or current.exposure is None:
            problems.append("both positive exposure denominators are required")
        if not baseline.exposure_unit or baseline.exposure_unit != current.exposure_unit:
            problems.append("exposure units are missing or different")
    if problems:
        return unavailable(problems)
    baseline_exact = Fraction(baseline.observed_count)
    observation_exact = Fraction(current.observed_count)
    if rule.comparison_basis == "exposure_normalized":
        # Use the declared decimal values, not binary float subtraction, so an
        # exact threshold remains inclusive without admitting nearby lower values.
        baseline_exact /= Fraction(str(baseline.exposure))
        observation_exact /= Fraction(str(current.exposure))
    try:
        baseline_value = float(baseline_exact)
        observation_value = float(observation_exact)
    except OverflowError:
        return unavailable(["normalized values are non-finite or the baseline rate is zero"])
    if (
        not math.isfinite(baseline_value)
        or not math.isfinite(observation_value)
        or baseline_value <= 0
    ):
        return unavailable(["normalized values are non-finite or the baseline rate is zero"])
    change_exact = 100 * (observation_exact - baseline_exact) / baseline_exact
    try:
        change = float(change_exact)
    except OverflowError:
        return unavailable(["relative change is not finite"])
    if not math.isfinite(change):
        return unavailable(["relative change is not finite"])
    directional_change = change_exact if rule.direction == "increase" else -change_exact
    if rule.direction == "either":
        directional_change = abs(change_exact)
    triggered = directional_change >= Fraction(str(rule.threshold_percent))
    return AlertEvaluation(
        rule_id=rule.id,
        status="review_needed" if triggered else "not_triggered",
        summary=(
            f"Observed relative change {change:.2f}% crosses the configured "
            f"{rule.threshold_percent:g}% {rule.direction} threshold; review the cited records."
            if triggered
            else f"Observed relative change {change:.2f}% does not cross the configured threshold."
        ),
        comparison_basis=rule.comparison_basis,
        threshold_percent=rule.threshold_percent,
        threshold_basis=rule.threshold_basis,
        baseline_value=baseline_value,
        observation_value=observation_value,
        relative_change_percent=change,
        record_ids=record_ids,
        method=method,
        coverage=coverage,
        origin=origin,
        limitations=tuple(limits),
    )


def illustrative_activity_example() -> tuple[AlertRule, ActivityWindow, ActivityWindow]:
    """A deterministic fictional example; 20 percent is not a validated default."""
    rule = AlertRule(
        id="illustration:explicit-20-percent",
        title="Illustrative activity change for human review",
        metric="records in a declared public-topic sample",
        threshold_percent=20,
        threshold_basis="illustrative_unvalidated",
        direction="increase",
        comparison_basis="equal_duration_counts",
        min_baseline_count=5,
        min_current_count=5,
        min_coverage_fraction=1,
    )
    start = datetime(2026, 11, 6, 10, tzinfo=UTC)
    common = {
        "metric": rule.metric,
        "source_ids": ("synthetic:public-source",),
        "timestamp_basis": "publication",
        "coverage_status": "complete",
        "coverage_fraction": 1,
        "collection_method": "Fictional fixture with known publication times",
        "synthetic": True,
    }
    baseline = ActivityWindow(
        id="illustration:baseline",
        start=start,
        end=start + timedelta(hours=1),
        observed_count=5,
        record_ids=tuple(f"illustration:baseline:{index}" for index in range(5)),
        **common,
    )
    current = ActivityWindow(
        id="illustration:observation",
        start=start + timedelta(hours=1),
        end=start + timedelta(hours=2),
        observed_count=6,
        record_ids=tuple(f"illustration:observation:{index}" for index in range(6)),
        **common,
    )
    return rule, baseline, current
