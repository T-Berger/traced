"""Who comments on the same post at the same time as whom.

Comments are sorted by (root post, time). For each comment, every later comment on the same
root within the window is a partner, found with one `searchsorted` instead of a loop.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _sorted_keys(c: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray]:
    d = c[c.root_id.notna()][["id", "root_id", "account", "t"]].copy()
    sec = (d.t - d.t.min()).dt.total_seconds().to_numpy().astype(np.int64)  # < 1e6 for one week
    root = pd.factorize(d.root_id)[0].astype(np.int64)
    key = root * 10_000_000 + sec  # int64 so the composite key stays exact
    order = np.argsort(key, kind="stable")
    return d.iloc[order].reset_index(drop=True), key[order]


def pairs(c: pd.DataFrame, window_s: float) -> pd.DataFrame:
    """Account pairs with the number of distinct root posts they shared within `window_s`."""
    d, key = _sorted_keys(c)
    acc, accounts = pd.factorize(d.account)
    root = pd.factorize(d.root_id)[0]
    n = len(d)
    ends = np.searchsorted(key, key + window_s, side="right")
    counts = ends - np.arange(n) - 1
    i = np.repeat(np.arange(n), counts)
    j = i + 1 + (np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts))
    a, b = acc[i], acc[j]
    keep = a != b
    lo, hi, r = np.minimum(a, b)[keep], np.maximum(a, b)[keep], root[i][keep]
    p = pd.DataFrame({"a": lo, "b": hi, "root": r}).drop_duplicates()
    out = p.groupby(["a", "b"]).size().rename("shared_posts").reset_index()
    out["a"] = accounts[out.a]
    out["b"] = accounts[out.b]
    return out


def crowding(c: pd.DataFrame, window_s: float) -> pd.Series:
    """Per comment: comments by OTHER accounts on the same root within +/- window_s."""
    d, key = _sorted_keys(c)
    total = np.searchsorted(key, key + window_s, "right") - np.searchsorted(key, key - window_s, "left")
    own_key = pd.factorize(d.account)[0].astype(np.int64) * 10**12 + key  # same account, same root
    own_sorted = np.sort(own_key)
    own = np.searchsorted(own_sorted, own_key + window_s, "right") - np.searchsorted(own_sorted, own_key - window_s, "left")
    return pd.Series(total - own, index=d.id.to_numpy())


def follows(c: pd.DataFrame) -> pd.DataFrame:
    """Per comment: who commented directly above it on the same root post (another account) and when.

    Consecutive comments by the same account form a run; every comment in a run follows the last
    comment before the run, so self-replies never count as following.
    Columns: leader, delta_s (seconds since the leader's comment), delta_norm (delta_s divided by the
    root post's median delta), indexed by comment id.
    """
    d, _ = _sorted_keys(c)
    new_root = d.root_id.ne(d.root_id.shift())
    run = (new_root | d.account.ne(d.account.shift())).cumsum()
    first = d.groupby(run).cumcount() == 0
    prev_t = d.t.shift().where(first & ~new_root)
    prev_acc = d.account.shift().where(first & ~new_root)
    d["leader"] = prev_acc.groupby(run).transform("first")
    d["delta_s"] = (d.t - prev_t.groupby(run).transform("first")).dt.total_seconds()
    med = d.groupby("root_id").delta_s.transform("median")
    d["delta_norm"] = d.delta_s / med.clip(lower=1)
    return d.set_index("id")[["account", "leader", "delta_s", "delta_norm"]]
