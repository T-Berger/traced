"""Similarity computations on stored embeddings (numpy only)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def echo_threshold(nn_sim: np.ndarray, length: np.ndarray, q: float, bins: int = 10) -> np.ndarray:
    """Per-text threshold: the q-quantile of best-match similarity among texts of similar length.

    A single random pair is the wrong null for a BEST match: the maximum over ~100k candidates
    clears any single-pair quantile almost surely (tried first: 90-96% of texts were flagged). So the
    comparison is like with like, best match against best match, within length deciles because short
    texts ("слава україні") naturally have closer matches than long ones.
    """
    edges = np.unique(np.quantile(length, np.linspace(0, 1, bins + 1)))
    b = np.clip(np.searchsorted(edges, length, side="right") - 1, 0, len(edges) - 2)
    thr = pd.Series(nn_sim).groupby(b).quantile(q)
    return thr.reindex(b).to_numpy()


def nearest_other(X: np.ndarray, account: np.ndarray, text: np.ndarray, k: int = 16, chunk: int = 1024) -> np.ndarray:
    """For every row, the highest cosine to a row from ANOTHER account with a DIFFERENT exact text.

    Exact copies are excluded because cross_dup_share already counts them; what is left is reworded
    copy. Exact search: X @ X.T in row chunks, keep the top k and take the first valid one.
    """
    best = np.full(len(X), np.nan, dtype=np.float32)
    for a in range(0, len(X), chunk):
        S = X[a:a + chunk] @ X.T
        top = np.argpartition(-S, k, axis=1)[:, :k]
        top_s = np.take_along_axis(S, top, axis=1)
        order = np.argsort(-top_s, axis=1)
        top, top_s = np.take_along_axis(top, order, axis=1), np.take_along_axis(top_s, order, axis=1)
        rows = np.arange(a, min(a + chunk, len(X)))
        valid = (account[top] != account[rows, None]) & (text[top] != text[rows, None])
        has = valid.any(axis=1)
        first = valid.argmax(axis=1)
        best[rows[has]] = top_s[has, first[has]]
        best[rows[~has]] = top_s[~has, -1]  # all top-k invalid: the k-th value is an upper bound
    return best


def centroids(X: np.ndarray, account: pd.Series) -> pd.DataFrame:
    """Mean direction of each account's comments (unit length)."""
    codes, names = pd.factorize(account)
    C = np.zeros((len(names), X.shape[1]), dtype=np.float32)
    np.add.at(C, codes, X)
    n = np.bincount(codes)
    C /= np.linalg.norm(C, axis=1, keepdims=True).clip(min=1e-9)
    return pd.DataFrame(C, index=names).assign(_n=n)


def self_similarity(X: np.ndarray, account: pd.Series) -> pd.Series:
    """Mean pairwise cosine among an account's own comments: (|sum v|^2 - n) / (n(n-1)) for unit v."""
    codes, names = pd.factorize(account)
    S = np.zeros((len(names), X.shape[1]), dtype=np.float64)
    np.add.at(S, codes, X)
    n = np.bincount(codes).astype(float)
    val = ((S**2).sum(axis=1) - n) / (n * (n - 1)).clip(min=1)
    return pd.Series(np.where(n >= 2, val, np.nan), index=names)
