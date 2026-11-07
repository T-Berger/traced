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

## Embeddings (one-time step, required for semantic metrics)

The semantic metrics read sentence vectors from `embeddings/`. That folder is git-ignored
(about 1 GB for both models) and is **not** built by `run.py`, so every teammate creates it once:

```sh
uvx uv@0.11.3 run --locked --package swarm-detector --group analysis --group embeddings \
    python swarm-detector/analysis/embed.py --model all --kind all
```

The `embeddings` group installs PyTorch and sentence-transformers; `run.py` only needs numpy.
On an Apple-silicon Mac (MPS) MiniLM takes minutes and e5 longer; the first run downloads the models.
If the vectors are missing, or were made from different comments (data or preprocessing changed),
`run.py` stops with the exact command to fix it (`--force` rebuilds). To run without them:
`run.py --no-semantic`. Only the default model is needed for a normal run:
`embed.py --model e5 --kind all` (about 14 min on an M-series Mac).

Why e5 + content is the default (comparison of all four variants, 2026-11-07): e5's closest
matches are real rewordings, also across Ukrainian and Russian; MiniLM's are mostly the same topic.
Context vectors include the post, so two comments under the same post look alike whatever they say:
`self_similarity` on context vectors mostly measures "comments on few posts" (ρ ≈ 0.5 with
active_days).

| option | values |
|---|---|
| `--model` | `minilm` = paraphrase-multilingual-MiniLM-L12-v2 (384 dims, 128-token window, fast) · `e5` = multilingual-e5-base (768 dims, window capped at 256 tokens) · `all` |
| `--kind` | `content` = the comment alone · `context` = `<comment> \|\| post: <first 400 chars of the post>` · `posts` = post texts (for post_relevance) · `all` |

In the context recipe the comment comes first, so when the window is full the post is cut, never
the comment. URLs become the word "link"; emoji-only, "+" and media-only comments get no vector.
Vectors are L2-normalised float16, saved with their ids and a JSON manifest (model, recipe, rows,
id fingerprint, timing, share of truncated texts).

Pick the vectors for a run with `--emb model:kind`, e.g. `run.py --emb minilm:context`. The default is
`e5:content` (set in `engine/config.py`) and writes to `results/`; other variants write to `results/<model>_<kind>/` so the
metric reports can be compared side by side.

## Reports

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
| `engine/embeddings.py` | embedding models, text recipes, loader with clear "how to create" errors |
| `engine/semantic.py` | nearest other-account match, random-pair threshold, centroids, self-similarity |
| `embed.py` | creates the stored vectors (the only file that needs PyTorch) |
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
