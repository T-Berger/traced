"""Raw metric table -> model-ready matrix Z, with a per-metric log of what was done.

Per metric j:  x -> T_j(x)  (log1p for heavy-tailed counts and durations)
               -> impute missing with the median
               -> z = (x - median) / s,  s = 1.4826 * MAD  (fallback IQR / 1.349, then sd)
               -> clip to [-5, 5]
The robust scale keeps the bulk of humans at unit spread so one hyperactive account cannot
stretch the axis for everyone else.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats

from .metrics import REGISTRY

CLIP = 5.0


@dataclass
class Normalised:
    raw: pd.DataFrame
    T: pd.DataFrame  # transformed + imputed, original units after the transform
    Z: pd.DataFrame  # robust z-scores, clipped
    log: pd.DataFrame


def bimodality_coefficient(x: np.ndarray) -> float:
    """BC = (g^2 + 1) / (k + 3(n-1)^2 / ((n-2)(n-3))). BC > 5/9 (uniform) hints at 2+ modes."""
    n = len(x)
    if n < 4 or np.std(x) == 0:
        return np.nan
    g = stats.skew(x, bias=False)
    k = stats.kurtosis(x, bias=False)
    return (g**2 + 1) / (k + 3 * (n - 1) ** 2 / ((n - 2) * (n - 3)))


def _transform(x: pd.Series, kind: str) -> pd.Series:
    if kind == "log1p":
        return np.log1p(x.clip(lower=0))
    if kind == "logit":
        p = x.clip(1e-3, 1 - 1e-3)
        return np.log(p / (1 - p))
    return x


def normalise(raw: pd.DataFrame) -> Normalised:
    T, Z, rows = {}, {}, []
    for col in raw:
        x = raw[col]
        kind = REGISTRY[col].transform
        t = _transform(x, kind)
        med = t.median()
        t = t.fillna(med)
        mad = stats.median_abs_deviation(t, scale="normal")
        q1, q3 = t.quantile([0.25, 0.75])
        if mad > 0:
            scale, how = mad, "MAD"
        elif q3 > q1:
            scale, how = (q3 - q1) / 1.349, "IQR"
        else:
            scale, how = t.std(), "SD"
        z = ((t - med) / scale).clip(-CLIP, CLIP)
        T[col], Z[col] = t, z
        rows.append({
            "metric": col, "group": REGISTRY[col].group, "transform": kind,
            "missing_%": x.isna().mean() * 100, "scale": how,
            "skew_raw": stats.skew(x.dropna()), "skew_after": stats.skew(z),
            "clipped_%": (((t - med) / scale).abs() > CLIP).mean() * 100,
            "zero_share_%": (x == 0).mean() * 100,
            "bimodality": bimodality_coefficient(t.to_numpy()),
        })
    return Normalised(raw, pd.DataFrame(T), pd.DataFrame(Z), pd.DataFrame(rows).set_index("metric"))
