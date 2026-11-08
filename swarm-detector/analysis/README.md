# Data exploration

`explore.py` profiles the Telegram challenge export (items, analyses, metadata) and writes
`explore_report.html`: technical sections first, plain English last.

Put `items.csv.gz`, `analyses.csv.gz` and `metadata_clean.json.gz` in `swarm-detector/data/`
(git-ignored), then from the repo root:

```sh
uv run --package swarm-detector --group analysis python swarm-detector/analysis/explore.py
```

Key finding: there is no account id column. Only `name_orig` identifies authors, and a
handle is embedded in it for roughly 27-40% of comments.
