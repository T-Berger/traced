# Evidence Explorer

A local Python 3.12/3.13 starter for source-backed news analysis: Dash, SQLite, a persistent background worker, and Ollama for BGE-M3 embeddings and Qwen claim extraction.

The included articles are **fictional demonstration fixtures**, identified by `.example` source URLs. They are not real reporting. Demo mode works offline and uses deterministic sample analysis; Ollama mode uses the configured local models without silently falling back to demo output.

## Shared review workspace

Use the shared workbench for agent investigations, Swarm findings, and historical media:

```bash
./scripts/start-trail.sh
```

Run this from the `evidence-explorer` directory. The launcher prepares the dedicated
`.venv-trail` environment and starts the shared desk with its combined worker. It
also works when invoked by absolute path from another directory: it changes into
the project directory before reading configuration. The equivalent manual launch is:

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/.venv-trail" uv sync --locked --dev --extra swarm
UV_PROJECT_ENVIRONMENT="$(pwd)/.venv-trail" uv run --locked --extra swarm \
  python -m evidence_explorer.review.integrations web --port 8053 --with-worker
```

Both Explorer and Trail use `EVIDENCE_DATA_DIR` from the process environment or
project `.env`, defaulting to `./data` and its `evidence.sqlite3` database. Process
environment values take precedence over `.env`. Relative data paths are resolved
from the project directory when using the launcher or the commands above. Use the
same selected directory for the collection dashboard and Trail so **Import current
Explorer** reads the saved articles and claims. With both interfaces open, run the
collection dashboard without its original worker; Trail's combined worker handles
article and review jobs for that directory.

Earlier launcher versions used `data/evidence-trail`. To open those saved cases in
place, select that directory explicitly:

```bash
EVIDENCE_DATA_DIR=./data/evidence-trail ./scripts/start-trail.sh
```

Use that same setting for Explorer when working with this separate store. Directory
selection opens its existing data; it does not move or merge data between stores.

Open [the shared workbench](http://127.0.0.1:8053). Its workflow is
**Cases → Review → Compare → Brief**, with reusable evidence inspectors and decision
controls across all three case types. It includes clearly labelled synthetic cases.
See the [review workflow guide](REVIEW_MVP.md) for draft recovery, assessment versions,
and portable briefing/case export. The article collection dashboard below remains
available for importing and analyzing news before sending it into review.

### Bring selected articles into Traced

1. Inspect an article in Explorer and choose **Add to Traced selection**.
   Repeat for each article you want to review.
2. Under **Use selected articles in Traced**, review the selection and choose
   **Preview selected articles**. Check the saved text versions, sources, and
   quoted claim counts, then choose **Download case package**. If evidence changes
   before download, preview the updated selection again.
3. In Traced, open **Cases → Gather evidence → Import case package** and choose
   the ZIP. Check the receipt and continue your saved investigation plan.
   **Attach collected evidence** and **Confirm scope** are separate actions.

The ZIP carries up to 20 selected saved articles and their exact quotation references. It can
be imported into a different Traced workspace. To snapshot an entire saved
Explorer collection in the receiving workspace, use **Advanced import**.

## Start the offline demo

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run from this directory:

```bash
bash scripts/run_demo.sh
```

This uses a dedicated `.venv-demo` with locked runtime dependencies, loads the sample corpus, and starts both processes. Open [http://127.0.0.1:8051](http://127.0.0.1:8051); Ctrl+C stops the dashboard and worker. It does not download models.

For separate terminals and development tools, use:

```bash
make -C .. setup
cp .env.example .env
uv run --locked evidence-explorer seed
uv run --locked evidence-explorer web --port 8051
```

Open [http://127.0.0.1:8051](http://127.0.0.1:8051). In a second terminal, start the worker:

```bash
uv run --locked evidence-explorer worker
```

The dashboard can search the sample articles immediately. The worker handles queued analysis and model-backed search. Keep both terminals open. If port 8051 is occupied, choose another port with `--port`. For the launcher, set `EVIDENCE_PORT` to the desired port.

Demo mode is intentionally labeled throughout the interface. It uses deterministic text features and source-grounded sample claims, not BGE-M3 or Qwen. It is suitable for trying the workflow without downloading models. Source links ending in `.example` are fictional and will not resolve to news websites.

## Use local models

Install and start [Ollama](https://ollama.com/download). On Apple Silicon, run Ollama natively on macOS so it can use Metal acceleration.

```bash
ollama pull bge-m3
ollama pull qwen3.5:9b
uv run --locked evidence-explorer doctor
uv run --locked evidence-explorer analyze --mode ollama
```

Keep the worker running while analysis is queued. Select Ollama mode in the dashboard after indexing completes. The default models are `bge-m3` for embeddings and `qwen3.5:9b` for structured claim extraction. Model downloads use several gigabytes of disk space. No cloud model account or API key is required.

For a smaller first live run, use `uv run --locked evidence-explorer analyze --mode ollama --article-id 1` to process one article, then inspect its quotations before analyzing the entire sample corpus.

Ollama mode fails with a visible job error if Ollama or a required model is unavailable; it never substitutes demo results. You can use another installed Qwen variant by setting `EVIDENCE_CHAT_MODEL` in `.env`. Changing an embedding model requires indexing for that model before semantic search can use it. The worker records model revisions in cache keys.

The initial settings use a bounded generation context and one worker so the laptop is not flooded with concurrent generation requests. Your 48 GB Apple Silicon laptop is a useful target for this setup; latency depends on article length, model quantization, and context. Start by analyzing the small sample corpus.

### Source meaning and attribution

Live extraction selects source excerpts rather than generating paraphrases. Qwen
returns only IDs from a bounded set of complete, unchanged excerpts; the app copies
the selected text into both the assertion and its quotation. Speaker labels,
questions that provide answer context, negation, uncertainty and unresolved
pronouns stay in their original wording. A speaker's name elsewhere in the article
does not establish whom a pronoun refers to. Inferred subject, speaker, event time
and location fields remain unknown.

Only excerpts fully contained in the first eight passages are eligible, with at
most 24 candidates, 800 characters per excerpt and three selections. Oversized or
clipped context is omitted; a question without an answer is not an assertion.
Unknown selection IDs get at most one corrective attempt. This conservative
extraction can omit useful claims and does not establish that a source is true.

The `claims-v2-source-excerpts` policy has its own cache revision. Reanalyzing an
unchanged article reuses its BGE embeddings but regenerates old free-form claims.
Deprecated Ollama v1 proposals remain available through explicit model-version
history, rather than appearing in current views. A successful extraction selects
the current result atomically, including an empty result; failures do not replace
it. Existing review snapshots retain their original provenance and decisions.

## Import articles and RSS feeds

Keep the worker running, then use **Add news** in the dashboard. Paste an **Article or feed URL**, choose **Article** or **RSS / Atom feed**, set the topic and feed limit, and select **Queue import →**. The **Analyze imported articles with local Ollama** checkbox starts checked; uncheck it for a source-only import. Collection runs in the worker, so fetching a slow source does not block the page. Feed imports attempt 1–20 distinct entries per request; failed entries count toward that limit.

You can also import from the command line. Replace these `.example` placeholders with actual public source URLs:

```bash
uv run --locked evidence-explorer ingest-url "https://publisher.example/article" \
  --topic "River flooding" --no-analyze

uv run --locked evidence-explorer ingest-feed "https://publisher.example/feed.xml" \
  --limit 5 --topic "River flooding" --no-analyze
```

For a small German-language feed import:

```bash
uv run --locked evidence-explorer ingest-feed \
  "https://www.tagesschau.de/infoservices/alle-meldungen-100~rss2.xml" \
  --limit 1 --topic "World news" --no-analyze
```

`--no-analyze` imports the article text, source metadata, and searchable passages without requiring Ollama. Omit that flag, or pass `--analyze`, to queue BGE-M3/Qwen analysis after collection. A missing local model produces a separate visible analysis error; successfully collected articles remain stored and searchable. There is no fallback to demo claims for real articles.

The importer follows public redirects, resolves feed links, removes common tracking parameters, and deduplicates article links. It records the requested URL, final source URL, feed URL when applicable, retrieval time, and available author/publication metadata. Unknown publication dates remain unknown instead of being replaced by the retrieval time.

Repeated imports update the same source record. Unchanged text retains its passages and cached analysis; changed text invalidates the derived records so later analysis uses the new source. When analysis is enabled, successfully collected articles are submitted to the existing cache-aware analysis pipeline, including previously imported sources that have not yet been analyzed.

A feed can finish with warnings when individual entries fail; inspect the job summary for successful imports and per-entry errors. Public HTML articles are supported; paywalls, authentication, JavaScript-only pages, PDFs, and publisher access restrictions are not bypassed. Private/local addresses, oversized responses, redirect loops, and network timeouts are rejected with visible errors.

## Saved feeds and public social posts

Use **Saved feeds → Add a saved feed** to choose a **Source platform**, enter its feed URL or channel/account locator, and set the topic, refresh interval, and item limit. RSS keeps the existing **Save feed →** action; platform channels use **Save source →**. For RSS, **News pages linked by feed** fetches the linked article pages. **Full posts from feed (social RSS)** reads RSS `content:encoded` or Atom content, falling back to the feed's description/summary when full content is absent. This mode never fetches post pages or links within posts. **Collection details** identifies whether saved text came from an article, full feed content, or a feed summary; summaries can be incomplete.

Saving an existing feed URL updates its settings and preserves its enabled or paused state. Claim generation selects unchanged source excerpts and allows a single corrective attempt for invalid selections; an exact match in the declared source passage is still required.

Mastodon provides [RSS feeds for public profiles and tags](https://docs.joinmastodon.org/user/network/#syndication-feeds). This public profile feed was verified with the collector:

```bash
uv run --locked evidence-explorer feeds add "Mastodon updates" \
  "https://mastodon.social/@Mastodon.rss" \
  --every-minutes 60 --limit 5 --social --no-analyze

uv run --locked evidence-explorer feeds list
uv run --locked evidence-explorer feeds refresh 1
uv run --locked evidence-explorer feeds pause 1
uv run --locked evidence-explorer feeds resume 1
```

Replace `1` with the ID returned when saving your feed. Omit `--social` for ordinary news feeds, and omit `--no-analyze` to run local Ollama analysis after each import. The refresh interval is 5–10,080 minutes. Keep the worker running for scheduled refreshes; saving a feed does not install an operating-system startup service. After downtime, one refresh catches up without replaying every missed interval. Failures apply bounded backoff and never poll more frequently than the configured schedule. **Refresh now**, **Pause**, and **Resume** are also available in the dashboard.

Public RSS/Atom from other social services or RSS bridges you are authorized to use works through the same standard feed interface. Only text supplied by the feed is collected; media, attachments, and linked articles are not downloaded in social RSS mode. A public post permalink and at least 30 characters of text are required; shorter or unavailable posts produce visible per-entry errors. Available author and publication metadata is retained; an update timestamp does not become a publication date, and missing metadata stays unknown.

Telegram, YouTube, Reddit, and X use platform adapters with the same saved-source schedule, SQLite storage, and optional local BGE-M3/Qwen analysis:

```bash
uv run --locked evidence-explorer feeds add "Telegram announcements" telegram \
  --platform telegram --limit 5 --no-analyze
uv run --locked evidence-explorer feeds add "NASA videos" UCLA_DiR1FfKNvjuUpBHmylQ \
  --platform youtube --limit 5 --no-analyze
# These require approved/configured API access first:
uv run --locked evidence-explorer feeds add "Reddit science" science --platform reddit --no-analyze
uv run --locked evidence-explorer feeds add "NASA on X" NASA --platform x --no-analyze
```

| Source | Supported collection | Access needed |
| --- | --- | --- |
| Telegram | Recent text and captions from a public channel's web preview; no complete archive, private channels, or media downloads | Public channel username or `https://t.me/CHANNEL`; no token. [Telegram public-post documentation](https://core.telegram.org/widgets/post) |
| YouTube | Recent public video titles and available descriptions from the channel Atom feed; no video, audio, captions, or transcripts | Channel ID beginning `UC` or `https://www.youtube.com/channel/CHANNEL_ID`; no API key. [YouTube Atom feed documentation](https://developers.google.com/youtube/data/guides/push_notifications) |
| Reddit | Latest bounded community post titles and text through the official OAuth API; no comments or private messages | Approved API access plus `REDDIT_ACCESS_TOKEN`, or `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET`; set a truthful `REDDIT_USER_AGENT`. [API requirements](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki) |
| X | A public account's available recent post text through official username lookup and user-post endpoints; no private messages or browser scraping | `X_BEARER_TOKEN` with endpoint access and any required usage credits. [User-post endpoint](https://docs.x.com/x-api/users/get-posts), [API access and billing](https://docs.x.com/x-api/getting-started/about-x-api) |

Put optional credentials in the server's `.env`, then restart the dashboard and worker. Credentials are never entered into saved-source records. A configured token means only that a credential is present; access is confirmed by the provider when collection runs. Missing credentials, rejected tokens, denied access, and rate limits produce visible source errors rather than sample results. Telegram previews and public feeds can also be unavailable or restricted.

Reddit requires explicit approval before API use; research access requires approval through its Researchers Program. Configure only an approved use case and observe the provider's data-removal and retention requirements. This starter does not implement a complete deletion-compliance service. See the current [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy). These four platform adapters do not provide account login, bypass private/restricted access, or fetch links embedded in collected posts.

## Commands

Run commands from the project directory:

| Command | Purpose |
| --- | --- |
| `uv run --locked evidence-explorer seed` | Idempotently load fictional articles and cached demo analysis |
| `uv run --locked evidence-explorer web --port 8051` | Start the dashboard; writable mode seeds the demo on first start |
| `uv run --locked evidence-explorer worker` | Run the single persistent analysis worker |
| `uv run --locked evidence-explorer worker --once` | Drain queued jobs and exit |
| `uv run --locked evidence-explorer analyze --mode demo` | Queue deterministic offline analysis |
| `uv run --locked evidence-explorer analyze --mode ollama` | Queue real BGE-M3 and Qwen analysis |
| `uv run --locked evidence-explorer ingest-url URL --no-analyze` | Queue a public article import without model inference |
| `uv run --locked evidence-explorer ingest-feed URL --limit 5` | Queue an RSS/Atom import followed by local Ollama analysis |
| `uv run --locked evidence-explorer feeds add NAME URL --every-minutes 60` | Save a feed for periodic refresh by the running worker |
| `uv run --locked evidence-explorer feeds list` | Inspect saved feeds, schedules, and latest results |
| `uv run --locked evidence-explorer search "bridge" --mode keyword` | Search saved passages with SQLite full-text search |
| `uv run --locked evidence-explorer doctor` | Check configuration, local models, data, and worker status |
| `uv run --locked evidence-explorer export artifacts/demo` | Export a consistent database snapshot for presentation |

Queueing a job does not execute it: run the worker in another terminal, or use `worker --once` after queueing it. Failed or interrupted jobs retain their error for inspection. Fix the cause and submit the action again; completed stages are reused when their input and model revision are unchanged.

## Configuration

Copy `.env.example` to `.env`. Environment variables override values in the file.

| Variable | Default | Purpose |
| --- | --- | --- |
| `EVIDENCE_DATA_DIR` | `./data` | Directory containing `evidence.sqlite3` |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Local Ollama endpoint |
| `EVIDENCE_EMBEDDING_MODEL` | `bge-m3` | Embedding model name |
| `EVIDENCE_CHAT_MODEL` | `qwen3.5:9b` | Claim extraction model name |
| `EVIDENCE_HOST` | `127.0.0.1` | Dashboard bind address |
| `EVIDENCE_PORT` | `8051` | Dashboard port; a hosting provider's `PORT` takes precedence |
| `EVIDENCE_READ_ONLY` | `false` | Disable database mutations in a presentation snapshot |

Store the working database on a local disk. Do not put a live SQLite database in a shared network or cloud-sync folder. Keep `.env`, database files, and model weights out of version control.

## Architecture

```text
Browser → Dash → SQLite jobs → one Python worker
             ↘ saved feeds     ├→ RSS/HTML, Telegram/YouTube, official Reddit/X APIs
                 and schedules └→ native Ollama for BGE-M3 and Qwen

SQLite stores articles, source provenance, passages, claims, embeddings and results.
```

The UI submits jobs and polls progress. The worker performs analysis outside dashboard callbacks. SQLite uses WAL mode and short transactions; each thread/process opens its own connection. Model requests never hold a database transaction.

| Module | Responsibility |
| --- | --- |
| `config.py`, `models.py` | Environment settings and validated domain records |
| `db.py`, `persistence/` | Article repository and FTS search; separate schema and durable job queue |
| `chunking.py`, `retrieval.py` | Source passages, deterministic demo features, retrieval ranking |
| `models_client.py` | Local Ollama HTTP adapter and model revision identity |
| `claim_excerpts.py`, `claim_selection.py`, `grounding.py` | Bounded source excerpts, typed ID-only selections, and source-preserving assertion validation |
| `analysis.py` | Analysis orchestration, quote validation, caching |
| `demo.py`, `data/sample_articles.json` | Fictional fixture corpus and precomputed demo records |
| `service.py` | Application operations shared by the dashboard and command line |
| `ingestion/` | Bounded HTTP collection, URL policy, article/feed parsing, and source provenance |
| `ingestion_contracts.py`, `ingestion_jobs.py` | Typed import requests/results, persistence, and follow-up analysis jobs |
| `ingestion_cli.py`, `ingestion_ui.py` | Thin command-line and Dash adapters for the ingestion service |
| `feeds/`, `feeds_ui.py` | Saved feed schedules, bounded refresh/backoff, and dashboard controls |
| `feeds/adapters/`, `collection/` | Saved-source conversion and the shared public/API platform collectors |
| `worker.py`, `cli.py` | Worker lifecycle and command-line entry points |
| `web.py`, `layout.py`, `ui.py`, `assets/` | Dash factory, callbacks, layout, rendering, and styles |
| `wsgi.py` | Explicit application startup for Gunicorn |

Claims retain supporting passage IDs and exact quotations. Similarity scores rank passages; they are not truth probabilities. Sample relationships and generated claims should be inspected against their evidence. Entity resolution and contradiction classification remain extension points.

To connect your own collection code, use `service.enqueue_ingestion(url, source_type="article", analyze=True)` or `source_type="feed"` with a bounded `limit`. For text you already collected, validate a record with `ArticleInput`, save it with `service.repo.upsert_article(record)`, then call `service.enqueue_analysis("ollama", article_ids=[article_id])`. The worker creates source passages, embeddings, and claims. Demo claim fixtures apply only to unchanged bundled samples; custom articles need Ollama for generated claims. Simply saving a URL as metadata does not fetch it; the explicit ingestion operation does.

## Docker and a hosted presentation

For live local inference on your Mac, use the native setup above. The Docker image is useful for a read-only presentation viewer and does not include Ollama or model weights.

Export the dataset, build the image, and mount the exported directory read-only:

```bash
uv run --locked evidence-explorer export artifacts/demo
docker build -f Dockerfile -t evidence-explorer ..
docker run --rm -p 8051:8050 \
  -e EVIDENCE_READ_ONLY=true \
  -v "$(pwd)/artifacts/demo:/app/data:ro" \
  evidence-explorer
```

The snapshot must exist before starting read-only mode. Export uses SQLite's backup mechanism instead of copying an active database file. Keyword search, stored claims, timelines, and the graph work from the snapshot. New semantic queries and fresh model analysis require a writable local application and its worker.

The image contains a read-only copy of the fictional sample corpus by default, so it also works without a volume mount. For Render or another container host, deploy the image as a web service. To publish another investigation, package its exported database into a derived image or provide it through the host's persistent storage at `EVIDENCE_DATA_DIR`. The server binds to the provider's `PORT` and exposes `/health`. A snapshot shipped inside an image is immutable; to retain writable application data across restarts, use persistent storage instead. The working local database is never synchronized by this starter automatically.

## Development

From the parent EDTH workspace, run `make check` to check all three applications and
their integration. It enables the optional local Swarm package so its bridge tests
run. See the [development guide](../docs/PYTHON_DEVELOPMENT.md) and [workspace guide](../README.md) for the module boundaries and full
folder map.

```bash
make -C .. test-explorer
make -C .. lint
```

Tests use temporary databases and mocked Ollama HTTP responses, so they run without internet or model downloads. They cover ingestion idempotency, invalidation after source changes, resilient search, durable jobs, interrupted-job reporting, snapshot reads, model failures, and exact source quotations.

The article/feed collector tests are also offline: `uv run --locked pytest tests/ingestion -q`. They use mocked HTTP responses and DNS answers to check metadata precision, RSS/Atom links, explicit social-feed content, partial failures, public redirects, address restrictions, and response-size/time limits.

Importing `evidence_explorer.web` exposes the application factory without opening a
database or seeding examples. Deployments should use `evidence_explorer.wsgi:server`;
the Docker command already points there.

A useful manual smoke check is: start the offline demo, search an article, inspect its quotation, enqueue demo analysis, run the worker, and confirm the job succeeds. Then stop Ollama and confirm an Ollama job reports a clear failure rather than displaying demo analysis as real model output.

## Troubleshooting

- **Worker offline / queued jobs:** start `uv run --locked evidence-explorer worker` from this project directory with the same `.env`.
- **Required model missing:** run the exact `ollama pull` command shown by `doctor`, then resubmit the analysis job.
- **Ollama connection refused:** start the native Ollama app/server and check `OLLAMA_BASE_URL`.
- **Another worker holds the lock:** stop the existing worker before starting another. The operating system releases its lock when it exits.
- **Read-only database missing:** export the database first and point `EVIDENCE_DATA_DIR` at the directory containing `evidence.sqlite3`.
- **Model generation is slow:** try `qwen3.5:4b`, analyze a smaller corpus, and let completed results remain cached.

This is a hackathon starter for evidence exploration. It does not assign automatic truth or disinformation verdicts.
