"""Regression coverage for the standalone theory analysis safety boundary."""

from __future__ import annotations

import csv
import gzip
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ANALYSIS = Path(__file__).resolve().parents[1] / "analysis"


@pytest.fixture
def isolated_analysis(tmp_path):
    analysis = tmp_path / "swarm-detector" / "analysis"
    analysis.mkdir(parents=True)
    for name in ("run.py", "embed.py"):
        shutil.copy2(ANALYSIS / name, analysis / name)
    shutil.copytree(
        ANALYSIS / "engine", analysis / "engine", ignore=shutil.ignore_patterns("__pycache__")
    )
    (analysis.parent / "data").mkdir()
    return analysis


def write_input(analysis, rows):
    with gzip.open(
        analysis.parent / "data" / "items.csv.gz", "wt", encoding="utf-8", newline=""
    ) as file:
        writer = csv.DictWriter(
            file, fieldnames=["id", "type", "name_orig", "published_at", "sender_id"]
        )
        writer.writeheader()
        writer.writerows(rows)


@pytest.mark.parametrize("entrypoint", ["run.py", "embed.py"])
@pytest.mark.parametrize(
    "rows",
    [
        [],
        [
            {
                "id": 1,
                "type": "SOCIAL_MEDIA_COMMENT",
                "name_orig": "shared7 Alice",
                "published_at": "",
            },
            {
                "id": 2,
                "type": "SOCIAL_MEDIA_COMMENT",
                "name_orig": "shared7 Alice",
                "published_at": "",
            },
        ],
        [
            {"id": 1, "type": "SOCIAL_MEDIA_COMMENT", "name_orig": "shared7", "sender_id": "17"},
            {"id": 2, "type": "SOCIAL_MEDIA_COMMENT", "name_orig": "shared7", "sender_id": ""},
        ],
    ],
)
def test_real_entrypoints_reject_unverified_identities_before_caches(
    isolated_analysis, entrypoint, rows
):
    write_input(isolated_analysis, rows)
    cache = isolated_analysis / "cache"
    cache.mkdir()
    old_cache = cache / "preprocessed.pkl"
    old_cache.write_bytes(b"invalid old cache must never be read")
    result = subprocess.run(
        [
            sys.executable,
            str(isolated_analysis / entrypoint),
            "--fresh" if entrypoint == "run.py" else "--force",
        ],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(isolated_analysis)},
    )
    assert result.returncode != 0
    assert "Account analysis unavailable" in result.stderr
    assert f"{len(rows)} are comments" in result.stderr
    assert "Traceback" not in result.stderr
    assert old_cache.read_bytes() == b"invalid old cache must never be read"
    assert not (isolated_analysis / "results").exists()
    assert not (isolated_analysis / "embeddings").exists()


def test_direct_preprocessing_is_guarded(isolated_analysis, monkeypatch):
    pytest.importorskip("numpy")
    pytest.importorskip("pandas")
    write_input(
        isolated_analysis, [{"id": 1, "type": "SOCIAL_MEDIA_COMMENT", "name_orig": "Alice"}]
    )
    monkeypatch.syspath_prepend(str(isolated_analysis))
    from engine import config, preprocess, readiness

    monkeypatch.setattr(config, "DATA", isolated_analysis.parent / "data")
    with pytest.raises(readiness.IdentityUnavailable, match="1 are comments"):
        preprocess.run()


def test_direct_metrics_context_rejects_cached_or_supplied_accounts(isolated_analysis, monkeypatch):
    pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    pytest.importorskip("networkx")
    write_input(
        isolated_analysis, [{"id": 1, "type": "SOCIAL_MEDIA_COMMENT", "name_orig": "Alice"}]
    )
    monkeypatch.syspath_prepend(str(isolated_analysis))
    from engine import config, metrics, readiness

    monkeypatch.setattr(config, "DATA", isolated_analysis.parent / "data")
    cached = pd.DataFrame({"account": ["guessed-name"], "in_model": [True]})
    with pytest.raises(readiness.IdentityUnavailable, match="1 are comments"):
        metrics.Ctx(cached)


def test_readiness_reports_bounded_sample(isolated_analysis):
    rows = [{"id": i, "type": "SOCIAL_MEDIA_COMMENT", "name_orig": "Alice"} for i in range(1001)]
    write_input(isolated_analysis, rows)
    result = subprocess.run(
        [sys.executable, str(isolated_analysis / "run.py")],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "first 1000 input records; 1000 are comments" in result.stderr
    assert not (isolated_analysis / "results").exists()


@pytest.mark.parametrize("entrypoint", ["run.py", "embed.py"])
def test_corrupted_gzip_reports_unavailable_without_traceback(isolated_analysis, entrypoint):
    data = bytearray(gzip.compress(b"type,name_orig\nSOCIAL_MEDIA_COMMENT,Alice\n"))
    data[10] = 0xFF
    (isolated_analysis.parent / "data" / "items.csv.gz").write_bytes(data)
    result = subprocess.run(
        [sys.executable, str(isolated_analysis / entrypoint)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Account analysis unavailable: cannot read items.csv.gz" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (isolated_analysis / "cache").exists()


def test_pair_matching_uses_exact_root_and_time(isolated_analysis, monkeypatch):
    np = pytest.importorskip("numpy")
    pytest.importorskip("pandas")
    pytest.importorskip("networkx")
    monkeypatch.syspath_prepend(str(isolated_analysis))
    from engine import web

    # The old floating-point composite keys made these two unrelated posts collide.
    i, j, _ = web._close_pairs(
        np.array([10000001.0, 0.0]),
        np.array([0, 1]),
        np.array([100, 101]),
        np.ones((2, 1)),
        "post",
        300,
    )
    assert len(i) == len(j) == 0
    # Large integer IDs retain their identity, even beyond float's exact integer range.
    roots = np.array([9007199254740992, 9007199254740993, 9007199254740992], dtype=np.int64)
    i, j, _ = web._close_pairs(
        np.array([0.0, 1.0, 2.0]), np.array([0, 1, 2]), roots, np.ones((3, 1)), "post", 300
    )
    assert list(zip(i, j, strict=True)) == [(0, 2)]


def test_sparse_web_inputs_are_unavailable(isolated_analysis, monkeypatch):
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    pytest.importorskip("networkx")
    monkeypatch.syspath_prepend(str(isolated_analysis))
    from engine import web

    with pytest.raises(ValueError, match="no eligible records"):
        web.build(pd.DataFrame(), np.empty((0, 1)), "any", set())
    single = pd.DataFrame({"t": pd.to_datetime(["2025-01-01T00:00:00Z"], utc=True)})
    with pytest.raises(ValueError, match="at least two days"):
        web.build(single, np.ones((1, 1)), "any", set())
    sparse = pd.DataFrame(
        {
            "t": pd.to_datetime(["2025-01-01T00:00:00Z", "2025-01-03T00:00:00Z"], utc=True),
            "account": ["a", "b"],
            "root_id": [1, 2],
            "text_len": [10, 20],
        }
    )
    with pytest.raises(ValueError, match="no close pairs"):
        web.build(sparse, np.ones((2, 1)), "post", set())
    equal = pd.DataFrame(
        {
            "t": pd.to_datetime(
                ["2025-01-01T00:00:00Z", "2025-01-01T00:00:10Z", "2025-01-03T00:00:00Z"], utc=True
            ),
            "account": ["a", "b", "c"],
            "root_id": [1, 1, 2],
            "text_len": [10, 10, 10],
        }
    )
    with pytest.raises(ValueError, match="text lengths do not vary"):
        web.build(equal, np.ones((3, 1)), "post", set())


def test_empty_shuffled_pair_sample_does_not_crash(isolated_analysis, monkeypatch):
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    pytest.importorskip("networkx")
    monkeypatch.syspath_prepend(str(isolated_analysis))
    from engine import web

    rows = pd.DataFrame(
        {
            "id": [1, 2, 3],
            "t": pd.to_datetime(
                ["2025-01-01T00:00:00Z", "2025-01-01T00:00:10Z", "2025-01-03T00:00:00Z"], utc=True
            ),
            "account": ["a", "b", "c"],
            "root_id": [1, 1, 2],
            "parent_id": [1, 1, 2],
            "parent_is_comment": [False, False, False],
            "text_len": [10, 20, 30],
        }
    )
    real = (np.array([0]), np.array([1]), np.array([1.0]))
    empty = (np.array([], dtype=int), np.array([], dtype=int), np.array([], dtype=float))
    calls = iter([real, empty])
    monkeypatch.setattr(web, "N_SHUFFLES", 1)
    monkeypatch.setattr(web, "_close_pairs", lambda *args, **kwargs: next(calls))
    assert web.build(rows, np.ones((3, 1)), "post", set()).tested == 0
