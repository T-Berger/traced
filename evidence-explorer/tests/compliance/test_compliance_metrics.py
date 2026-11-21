from datetime import UTC, datetime, timedelta

from evidence_explorer.alerts.settings import AlertSettings
from evidence_explorer.compliance import metrics, ui

CAPTURE = datetime(2026, 11, 7, 12, tzinfo=UTC)
START = datetime(2025, 10, 6, 9, tzinfo=UTC)  # a Monday


def _row(n, *, day=0, racism=0.0, views=1000, reposts=10, attribution="third_party", **extra):
    return {
        "id": f"p{n}",
        "status": "classified",
        "attribution": attribution,
        "disinformation": 0.0,
        "racism": racism,
        "hate_speech": 0.0,
        "harassment": 0.0,
        "posted_at": START + timedelta(days=day),
        "collected_at": CAPTURE,
        "view_count": views,
        "like_count": 5,
        "repost_count": reposts,
        "reply_count": 5,
        "followers_count": 100,
        "author_id": f"a{n % 3}",
        "handle": f"user{n % 3}",
        **extra,
    }


def _audit(rows, bursts=None):
    return metrics.build_audit(rows, AlertSettings.defaults(), bursts)


def test_subject_posts_and_unclassified_rows_stay_out_of_both_groups():
    rows = [
        _row(1, racism=0.9),
        _row(2),
        _row(3, racism=0.9, attribution="subject_own"),
        _row(4, status="pending", racism=0.9),
    ]
    audit = _audit(rows)
    assert [p.get("id") for p in audit.flagged] == ["p1"]
    assert [p.get("id") for p in audit.baseline] == ["p2"]
    assert audit.excluded == 2


def test_reach_and_share_indices_compare_flagged_with_baseline_medians():
    rows = [_row(i, views=500, reposts=20, racism=0.9) for i in range(3)]
    rows += [_row(10 + i, views=1000, reposts=10) for i in range(3)]
    s = metrics.summary(_audit(rows))
    assert s["reach_index"] == 0.5
    assert s["share_index"] == 4.0  # 40 vs 10 reposts per 1K views
    assert s["flagged_views"] == 1500
    racism = next(r for r in metrics.category_matrix(_audit(rows)) if r["category"] == "racism")
    assert racism["posts"] == 3 and racism["reach_index"] == 0.5


def test_missing_views_do_not_count_as_zero_reach():
    rows = [_row(1, racism=0.9, views=None), _row(2, racism=0.9, views=800), _row(3, views=400)]
    s = metrics.summary(_audit(rows))
    assert s["views_unknown"] == 1
    assert s["median_views_flagged"] == 800
    assert metrics.view_samples(_audit(rows))["flagged"] == [800.0]


def test_days_live_measure_posting_to_capture():
    audit = _audit([_row(1, racism=0.9, day=0), _row(2, racism=0.9, day=10)])
    days = metrics.persistence(audit)
    assert round(min(days)) == (CAPTURE - START).days - 10
    assert metrics.summary(audit)["min_days_live"] == min(days)


def test_weekly_series_flags_spike_weeks_from_normalised_share():
    rows = []
    n = 0
    for week in range(8):
        flagged = 8 if week == 5 else 1
        for i in range(10):
            rows.append(_row(n, day=7 * week + i % 5, racism=0.9 if i < flagged else 0.0))
            n += 1
    weeks = metrics.weekly_series(_audit(rows))
    assert len(weeks) == 8
    assert all(w["week"].weekday() == 0 for w in weeks)
    assert [w["spike"] for w in weeks].index(True) == 5
    assert sum(w["spike"] for w in weeks) == 1
    assert weeks[5]["rate"] == 0.8
    assert metrics.summary(_audit(rows))["spike_weeks"] == 1


def test_periods_bucket_by_month_with_quarter():
    rows = [_row(1, racism=0.9, day=0), _row(2, day=40), _row(3, racism=0.9, day=100)]
    labels = [p["period"] for p in metrics.periods(_audit(rows))]
    assert labels == ["Oct 2025 (Q4)", "Nov 2025 (Q4)", "Jan 2026 (Q1)"]


def test_top_accounts_rank_by_flagged_views():
    rows = [_row(0, racism=0.9, views=50), _row(1, racism=0.9, views=900), _row(4, views=10**6)]
    top = metrics.top_accounts(_audit(rows))
    assert [a["handle"] for a in top] == ["user1", "user0"]


def test_every_chart_renders_for_empty_and_populated_audits():
    for rows in ([], [_row(i, day=i * 3, racism=0.9 if i % 4 == 0 else 0.0) for i in range(30)]):
        audit = _audit(rows, [{"window_start": START, "window_end": START + timedelta(minutes=30),
                               "post_count": 5, "reply_count": 4, "author_handle": "x",
                               "target": "y"}])  # fmt: skip
        weeks = metrics.weekly_series(audit)
        matrix = metrics.category_matrix(audit)
        for fig in (
            ui.velocity_figure(weeks),
            ui.volatility_figure(weeks),
            ui.ecdf_figure(metrics.view_samples(audit)),
            ui.reach_time_figure(weeks),
            ui.index_figure(matrix),
            ui.persistence_figure(metrics.persistence(audit)),
            ui.periods_figure(metrics.periods(audit)),
            ui.bursts_figure(audit.bursts),
        ):
            assert fig.to_dict()["layout"]
        if rows:
            s = metrics.summary(audit)
            ui._summary({"subject_name": "Subject"}, s)
            gaps = ui._gaps(s, matrix, {"window_end": datetime(2026, 3, 31).date()})
            assert "Coverage" in gaps.children[0].children
