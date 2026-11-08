"""Score every metric: does the model use it, does it separate populations, is it redundant.

The reference partition is the BIC-best Gaussian mixture. Because that partition is learnt from
the same metrics, "importance" here means "how much the unsupervised structure depends on this
metric", not "how well it finds bots": there is no ground truth in the data.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.ensemble import RandomForestClassifier
from sklearn.feature_selection import mutual_info_classif
from sklearn.inspection import permutation_importance
from sklearn.metrics import adjusted_rand_score
from sklearn.mixture import GaussianMixture
from sklearn.model_selection import train_test_split

from . import config as cfg
from .models import REG, Latent, Mixture

CRITERIA = {
    "fisher": "Fisher ratio: between-component variance / within-component variance",
    "mutual_info": "Mutual information (nats) between the metric and the mixture component",
    "perm_importance": "Drop in surrogate-forest accuracy when the metric is shuffled",
    "loo_dependence": "1 - ARI between mixture labels with and without the metric",
    "anomaly_ks": "KS distance between flagged anomalies and everyone else",
    "communality": "Share of the metric's variance captured by the PCs that explain 80%",
}


def fisher_ratio(x: np.ndarray, lab: np.ndarray) -> float:
    mu = x.mean()
    between = sum((lab == k).sum() * (x[lab == k].mean() - mu) ** 2 for k in np.unique(lab))
    within = sum(((x[lab == k] - x[lab == k].mean()) ** 2).sum() for k in np.unique(lab))
    return float(between / within) if within > 0 else np.nan


def score(Z: pd.DataFrame, mix: Mixture, lat: Latent, anomaly_flag: np.ndarray) -> pd.DataFrame:
    lab = mix.labels
    X = Z.to_numpy()
    out = pd.DataFrame(index=Z.columns)
    out["fisher"] = [fisher_ratio(X[:, j], lab) for j in range(X.shape[1])]
    out["mutual_info"] = mutual_info_classif(X, lab, random_state=cfg.SEED)

    Xtr, Xte, ytr, yte = train_test_split(X, lab, test_size=0.3, stratify=lab, random_state=cfg.SEED)
    rf = RandomForestClassifier(300, max_depth=10, n_jobs=-1, random_state=cfg.SEED).fit(Xtr, ytr)
    pi = permutation_importance(rf, Xte, yte, n_repeats=8, random_state=cfg.SEED, n_jobs=-1)
    out["perm_importance"] = pi.importances_mean
    out.attrs["surrogate_accuracy"] = rf.score(Xte, yte)

    loo = []
    for col in Z:
        g = GaussianMixture(mix.k_best, covariance_type="full", reg_covar=REG, n_init=3,
                            random_state=cfg.SEED).fit(Z.drop(columns=col))
        loo.append(1 - adjusted_rand_score(lab, g.predict(Z.drop(columns=col))))
    out["loo_dependence"] = loo

    out["anomaly_ks"] = [stats.ks_2samp(X[anomaly_flag, j], X[~anomaly_flag, j]).statistic for j in range(X.shape[1])]
    out["communality"] = (lat.loadings.iloc[:, :lat.n_for_80] ** 2).sum(axis=1).clip(upper=1)

    rho = Z.corr(method="spearman").abs()
    rho = rho.mask(np.eye(len(rho), dtype=bool))
    out["max_corr"] = rho.max()
    out["most_similar"] = rho.idxmax()

    ranks = out[list(CRITERIA)].rank(pct=True)
    out["overall"] = ranks[["fisher", "mutual_info", "perm_importance", "loo_dependence", "anomaly_ks"]].mean(axis=1)
    return out.sort_values("overall", ascending=False)
