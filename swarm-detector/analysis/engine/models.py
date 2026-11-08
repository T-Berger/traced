"""The five unsupervised views of the account x metric matrix Z.

Each function returns plain numbers and arrays; plotting lives in the report scripts so a
model can be rerun or swapped without touching the visuals.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.cluster import HDBSCAN, KMeans
from sklearn.decomposition import PCA, FactorAnalysis
from sklearn.ensemble import IsolationForest
from sklearn.metrics import roc_auc_score, silhouette_score
from sklearn.mixture import BayesianGaussianMixture, GaussianMixture
from sklearn.neighbors import KernelDensity

from . import config as cfg

K_RANGE = range(1, 13)
# Covariance floor added to every component's diagonal, in robust-z units. Many metrics are
# zero-inflated (90% exact zeros); with a tiny floor the mixture spends components on those point
# masses and BIC keeps falling. 0.1 (sd ~ 0.3) says "differences below a third of a typical spread
# are not structure". The report shows how k* moves with this choice.
REG = 0.1
REG_SENSITIVITY = (0.01, 0.1, 0.3)


# ---------------------------------------------------------------- latent variables

@dataclass
class Latent:
    pca: PCA
    scores: np.ndarray  # n x p
    loadings: pd.DataFrame  # metric x component (correlation of metric with component)
    fa_loadings: pd.DataFrame
    n_for_80: int


def latent(Z: pd.DataFrame, n_factors: int = 4) -> Latent:
    pca = PCA(random_state=cfg.SEED).fit(Z)
    scores = pca.transform(Z)
    load = pca.components_.T * np.sqrt(pca.explained_variance_) / Z.std(ddof=0).to_numpy()[:, None]
    cols = [f"PC{i + 1}" for i in range(Z.shape[1])]
    fa = FactorAnalysis(n_components=n_factors, rotation="varimax", random_state=cfg.SEED).fit(Z / Z.std())
    n80 = int(np.searchsorted(np.cumsum(pca.explained_variance_ratio_), 0.8) + 1)
    return Latent(pca, scores, pd.DataFrame(load, index=Z.columns, columns=cols),
                  pd.DataFrame(fa.components_.T, index=Z.columns, columns=[f"F{i + 1}" for i in range(n_factors)]),
                  n80)


# ---------------------------------------------------------------- mixtures

@dataclass
class Mixture:
    bic: pd.Series
    aic: pd.Series
    k_best: int
    gmm: GaussianMixture
    labels: np.ndarray
    logp: np.ndarray  # log p(x) under the best model
    gmm2: GaussianMixture
    dp_weights: np.ndarray
    dp_effective: int
    reg_sensitivity: pd.DataFrame  # reg_covar x k -> BIC - min BIC


def mixtures(Z: pd.DataFrame) -> Mixture:
    fits = {k: GaussianMixture(k, covariance_type="full", reg_covar=REG, n_init=3, random_state=cfg.SEED).fit(Z)
            for k in K_RANGE}
    sens = {}
    for reg in REG_SENSITIVITY:
        b = pd.Series({k: (fits[k] if reg == REG else GaussianMixture(
            k, covariance_type="full", reg_covar=reg, n_init=2, random_state=cfg.SEED).fit(Z)).bic(Z) for k in K_RANGE})
        sens[reg] = b - b.min()
    bic = pd.Series({k: m.bic(Z) for k, m in fits.items()})
    aic = pd.Series({k: m.aic(Z) for k, m in fits.items()})
    k_best = int(bic.idxmin())
    best = fits[k_best]
    dp = BayesianGaussianMixture(n_components=max(K_RANGE), covariance_type="full", reg_covar=REG,
                                 weight_concentration_prior_type="dirichlet_process",
                                 weight_concentration_prior=0.01, max_iter=500, n_init=2,
                                 random_state=cfg.SEED).fit(Z)
    w = np.sort(dp.weights_)[::-1]
    return Mixture(bic, aic, k_best, best, best.predict(Z), best.score_samples(Z), fits[2], w,
                   int((w > 0.01).sum()), pd.DataFrame(sens).T)


# ---------------------------------------------------------------- clustering

@dataclass
class Clustering:
    silhouette: pd.Series
    k_best: int
    kmeans_labels: np.ndarray
    hdb_labels: np.ndarray
    hdb_prob: np.ndarray


def clustering(Z: pd.DataFrame, scores: np.ndarray, n_dims: int) -> Clustering:
    rng = np.random.default_rng(cfg.SEED)
    sample = rng.choice(len(Z), size=min(3000, len(Z)), replace=False)
    sil, labels = {}, {}
    for k in range(2, 9):
        lab = KMeans(k, n_init=10, random_state=cfg.SEED).fit_predict(Z)
        sil[k] = silhouette_score(Z.to_numpy()[sample], lab[sample])
        labels[k] = lab
    sil = pd.Series(sil)
    k_best = int(sil.idxmax())
    # HDBSCAN in the reduced PCA space: density is meaningless in 21 noisy dimensions
    hdb = HDBSCAN(min_cluster_size=15, min_samples=5, copy=True).fit(scores[:, :n_dims])
    return Clustering(sil, k_best, labels[k_best], hdb.labels_, hdb.probabilities_)


# ---------------------------------------------------------------- density

def kde_1d(x: np.ndarray, grid: int = 200) -> tuple[np.ndarray, np.ndarray, float]:
    """Gaussian KDE with Silverman bandwidth h = 0.9 min(sd, IQR/1.34) n^(-1/5)."""
    x = x[np.isfinite(x)]
    sd, iqr = x.std(), stats.iqr(x) / 1.34
    h = 0.9 * min(sd, iqr if iqr > 0 else sd) * len(x) ** (-0.2) or 1e-3
    xs = np.linspace(x.min() - 3 * h, x.max() + 3 * h, grid)
    kde = KernelDensity(bandwidth=h).fit(x[:, None])
    return xs, np.exp(kde.score_samples(xs[:, None])), h


def kde_2d(pts: np.ndarray, grid: int = 80) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    h = 1.06 * pts.std(axis=0).mean() * len(pts) ** (-1 / 6)
    kde = KernelDensity(bandwidth=h).fit(pts)
    lo, hi = np.percentile(pts, [0.5, 99.5], axis=0)
    gx, gy = np.linspace(lo[0], hi[0], grid), np.linspace(lo[1], hi[1], grid)
    xx, yy = np.meshgrid(gx, gy)
    dens = np.exp(kde.score_samples(np.c_[xx.ravel(), yy.ravel()])).reshape(xx.shape)
    return gx, gy, dens


# ---------------------------------------------------------------- anomaly

@dataclass
class Anomaly:
    gmm_score: np.ndarray  # -log p(x): higher = rarer
    iforest_score: np.ndarray  # higher = easier to isolate
    combined: np.ndarray  # mean of the two percentile ranks
    flag: np.ndarray  # top share by combined score
    spearman: float
    top_overlap: float


def anomaly(Z: pd.DataFrame, logp: np.ndarray, top: float = 0.02) -> Anomaly:
    iso = IsolationForest(n_estimators=400, random_state=cfg.SEED).fit(Z)
    s_if = -iso.score_samples(Z)
    s_gmm = -logp
    r_if, r_gmm = stats.rankdata(s_if) / len(Z), stats.rankdata(s_gmm) / len(Z)
    comb = (r_if + r_gmm) / 2
    cut = 1 - top
    a, b = r_if > cut, r_gmm > cut
    return Anomaly(s_gmm, s_if, comb, comb >= np.quantile(comb, cut),
                   float(stats.spearmanr(s_if, s_gmm).statistic), float((a & b).sum() / max(a.sum(), 1)))


# ---------------------------------------------------------------- separation

@dataclass
class Separation:
    delta_bic: float  # BIC(k=1) - BIC(k=2): > 10 is very strong evidence for two populations
    bhattacharyya: float
    bayes_error_bound: float
    weights: np.ndarray
    labels: np.ndarray
    per_metric: pd.DataFrame


def bhattacharyya(m1, S1, m2, S2) -> float:
    S = (S1 + S2) / 2
    d = m1 - m2
    _, ld = np.linalg.slogdet(S)
    _, ld1 = np.linalg.slogdet(S1)
    _, ld2 = np.linalg.slogdet(S2)
    return float(d @ np.linalg.solve(S, d) / 8 + 0.5 * (ld - 0.5 * (ld1 + ld2)))


def separation(Z: pd.DataFrame, mix: Mixture) -> Separation:
    g = mix.gmm2
    lab = g.predict(Z)
    # name component 0 the larger ("dominant") one
    if (lab == 0).sum() < (lab == 1).sum():
        lab = 1 - lab
        order = [1, 0]
    else:
        order = [0, 1]
    m = g.means_[order]
    S = g.covariances_[order]
    w = g.weights_[order]
    db = bhattacharyya(m[0], S[0], m[1], S[1])
    rows = []
    for col in Z:
        a, b = Z[col].to_numpy()[lab == 0], Z[col].to_numpy()[lab == 1]
        pooled = np.sqrt(((len(a) - 1) * a.var(ddof=1) + (len(b) - 1) * b.var(ddof=1)) / (len(a) + len(b) - 2))
        ks = stats.ks_2samp(a, b)
        rows.append({"metric": col, "ks": ks.statistic, "ks_p": ks.pvalue,
                     "cohen_d": (b.mean() - a.mean()) / pooled if pooled > 0 else 0.0,
                     "auc": roc_auc_score(lab, Z[col]),
                     "mean_dominant": a.mean(), "mean_other": b.mean()})
    return Separation(float(mix.bic[1] - mix.bic[2]), db, float(np.sqrt(w[0] * w[1]) * np.exp(-db)), w, lab,
                      pd.DataFrame(rows).set_index("metric"))
