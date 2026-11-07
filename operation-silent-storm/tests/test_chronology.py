from __future__ import annotations

import pytest
from test_ingest import frame, release

from silent_storm.chronology import MAX_WEEK_BINS
from silent_storm.ingest import import_release
from silent_storm.reviews import ReviewService
from silent_storm.storage import Store


def test_wide_week_span_keeps_actual_bins_without_unbounded_gap_expansion(tmp_path):
    root = release(tmp_path, frames=[frame("a", week="Week 1"), frame("b", week="Week 1000000")])
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, root)
    service = ReviewService(store)
    baseline = service.ensure_baseline("day-1")
    chart = service.chronology(baseline["scenario_id"])
    assert chart["gap_fill_limited"] is True
    assert [item["week"] for item in chart["bins"]] == [1, 1000000]


def test_too_many_distinct_labels_request_schema_review_instead_of_dropping_records(tmp_path):
    rows = [frame(f"f{week}", week=f"Week {week}") for week in range(MAX_WEEK_BINS + 1)]
    root = release(tmp_path, frames=rows)
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, root)
    service = ReviewService(store)
    baseline = service.ensure_baseline("day-1")
    with pytest.raises(ValueError, match="bounded chart"):
        service.chronology(baseline["scenario_id"])


def test_chronology_uses_supplied_metadata_without_scanning_media(tmp_path, monkeypatch):
    root = release(tmp_path, frames=[frame("a", week=""), frame("b", video_id="", week="")])
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, root)
    service = ReviewService(store)
    baseline = service.ensure_baseline("day-1")
    (root / "frames/a.jpg").unlink()

    def forbidden_stat(*_args, **_kwargs):
        raise AssertionError("Chronology must not inspect every media file")

    monkeypatch.setattr(store, "get_frame", forbidden_stat)
    chart = service.chronology(baseline["scenario_id"])
    assert chart["x_label"] == "Exercise week first posted"
    assert chart["bins"][0]["week"] is None
    assert chart["bins"][0]["frame_count"] == 2
    assert chart["bins"][0]["video_count"] == 1
