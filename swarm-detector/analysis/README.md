# Analysis

Two parts:

- `explore.py` profiles the raw export (items, analyses, metadata) and writes `explore_report.html`.
- `run.py` + `engine/` is the theory pipeline: preprocessing, pluggable account metrics, five
  unsupervised model families, and a per-metric evaluation. It writes three linked reports to
  `results/` plus `results/accounts.csv.gz` (every metric, cluster label and anomaly score per account).

Put `items.csv.gz`, `analyses.csv.gz` and `metadata_clean.json.gz` in `swarm-detector/data/`
(git-ignored), then from the repo root:

```sh
uvx uv@0.11.3 run --locked --package swarm-detector --group analysis python swarm-detector/analysis/run.py
```

`--fresh` rebuilds the cached preprocessing in `cache/` (git-ignored). About 30 s end to end.
`results/` is git-ignored too: the reports show real Telegram names next to anomaly scores and
this repo is public, so regenerate them locally.
The reports load Plotly and KaTeX from jsDelivr, so open them with a network connection.

| report | content |
|---|---|
| `results/01_preprocessing.html` | every cleaning step with rows in/out and why, identity, text, threads, feature normalisation |
| `results/02_models.html` | latent variables (PCA, FA), Gaussian mixtures + BIC + Dirichlet process, K-means/HDBSCAN, KDE, anomaly detection, separation (ΔBIC, Bhattacharyya, Bayes-error bound) |
| `results/03_metrics.html` | per-metric importance, separability, redundancy, metric cards |

## Engine

| file | role |
|---|---|
| `engine/config.py` | paths and every analyst choice (thresholds, windows, channel sides) |
| `engine/preprocess.py` | raw export -> clean comments, logging each step |
| `engine/coactivity.py` | same-post, same-time account pairs (vectorised) |
| `engine/metrics.py` | metric registry: `@metric(...)` functions returning one value per account |
| `engine/features.py` | transform, impute, robust z-score; per-metric log |
| `engine/models.py` | latent, mixtures, clustering, density, anomaly, separation |
| `engine/evaluate.py` | Fisher ratio, mutual information, permutation and leave-one-out importance, anomaly KS, redundancy |
| `engine/page.py` | HTML builder: KaTeX maths, Plotly figures with a hover "what to look for" |

To add a metric, write one function in `engine/metrics.py`:

```python
@metric("timing", transform="log1p",
        description="Median seconds from the parent message to the reply.",
        hypothesis="Scripted accounts answer on a trigger, so the delay is short and steady.",
        look_for="A separate low mode: a group that always answers within seconds.")
def median_parent_latency(ctx):
    return ctx.c.groupby("account").latency_parent_s.median()
```

The next run normalises it, feeds it to every model and scores it in the metrics report.

Key data facts: there is no account id column; the handle embedded in `name_orig` is used where
present (about 31% of comments), otherwise the display name. Channel sides (ua / ru) are our own
labelling in `engine/config.py`.
