# Analysis

This directory contains two separate tools:

- `explore.py` profiles the supplied export and writes `explore_report.html`.
- `run.py`, `embed.py`, and `engine/` contain a proposed account-level theory pipeline. **Account
  analysis and embedding generation are currently unavailable.**

The supplied export has no verified source-native sender ID. `name_orig` is a display field;
matching display names and tokens resembling handles do not establish that two records came from
one account. Source-scoped account mapping, complete declared inputs and background coverage
must be established and reviewed before account analysis can run. Channel categories in
`engine/config.py` are analyst labels, not source-provided affiliation or a depicted side.

`run.py` and `embed.py` report this limitation after inspecting at most the first 1,000 input
records. The count describes that sample only. Both commands stop before loading older cached
preprocessing, downloading a model, generating embeddings, or writing new analysis output. An
arbitrary additional ID-like column is not treated as an established sender identity. `--fresh`,
`--force`, and `--no-semantic` do not bypass the gate.

Any previously generated account metrics, graphs, and reports may contain merged or incorrectly
attributed identities. They remain on disk for inspection and are **not validated evidence**.
They are not deleted or updated by the gated commands. `cache/`, `results/`, and `embeddings/`
are git-ignored; results may include real names and should remain local.

To inspect the current availability message, put `items.csv.gz` in `swarm-detector/data/` and run:

```sh
uvx uv@0.11.3 run --locked --package swarm-detector --group analysis \
    python swarm-detector/analysis/run.py
```

The proposed embedding command is also gated; it does not download a model:

```sh
uvx uv@0.11.3 run --locked --package swarm-detector --group analysis --group embeddings \
    python swarm-detector/analysis/embed.py --model e5 --kind all
```

`run.py` and `embed.py` retain their implementation for review, but their legacy descriptions,
model outputs, analysis thresholds, and saved caches have **not** been validated as account
analysis. No human decision, intent, operator, automation, or independent corroboration follows
from similarity, timing, clustering, or a model score.

## Proposed pipeline, currently unavailable

The existing code is organized as follows. The descriptions indicate proposed responsibilities,
not a claim that the reports or metrics can currently be generated safely.

| File | Proposed role |
|---|---|
| `engine/readiness.py` | Fail closed when a supported source-identity contract is unavailable. |
| `engine/config.py` | Paths and analyst choices, including declared thresholds and channel labels. |
| `engine/preprocess.py` | Transform a source export into comments with recorded preprocessing steps. |
| `engine/coactivity.py` | Find accounts commenting on a shared post within a declared time window. |
| `engine/metrics.py` | Register and calculate account-level measurements. |
| `engine/features.py` | Transform, impute, and normalize the measurements. |
| `engine/models.py` | Fit latent-variable, mixture, clustering, and anomaly models. |
| `engine/evaluate.py` | Compare and inspect metrics and model outputs. |
| `engine/embeddings.py`, `embed.py` | Prepare and load explicitly selected local text vectors. |
| `engine/semantic.py`, `engine/web.py` | Compare content and timing, including randomized timing checks. |
| `engine/page.py` | Construct proposed HTML reports. |

The original design described preprocessing, models, metrics, and timing/meaning web reports
plus an `accounts.csv.gz` export. These are **not currently produced**. Re-enabling them also
requires review of source coverage, method assumptions, wording, and exact evidence handoffs; a
source identity column alone does not establish those properties. Generated HTML would load Plotly
and KaTeX from jsDelivr, so an offline report would require additional work.

To exercise the identity boundary and timing-pair regressions without downloading models:

```sh
uv --directory swarm-detector run --locked --all-packages --all-extras \
    --group dev --group analysis pytest tests/test_theory_safety.py -q
```
