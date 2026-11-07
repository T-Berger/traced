# Traced evidence workbench

This workspace contains three installable Python applications. Evidence Explorer
hosts the shared Evidence Trail review flow; Swarm Detector supplies specialist
behavioral analysis; Silent Storm provides historical-media inspection, review,
and posting chronology. Their shared workflow is **plan investigation → gather
evidence → confirm scope → investigate findings → review conclusions → prepare
and share briefing**.

## Start here

Use Python 3.12 and [uv 0.11.3](https://docs.astral.sh/uv/). Python 3.12 is the common
supported version: Evidence Explorer also supports 3.13, while Swarm Detector
and Silent Storm currently require 3.12.

From this workspace folder, launch the integrated review desk:

```sh
./traced start
```

Open [the shared workbench](http://127.0.0.1:8053) and follow
**Cases → Review → Compare → Briefing**. On Cases, choose **Start investigation**
to open the editor, then **Save plan**. Follow **Gather evidence** and continue
the saved plan with the selected case. Review and save its sources, explicitly
attach the evidence, and confirm scope. **Open first finding** then takes you
to the first finding awaiting a decision. Comparisons and
specialist checks are optional routes during investigation. See the
[planning walkthrough](docs/INVESTIGATION_PLANNING.md) for the full sequence.

`EDTH` is the common project root:
`./traced` uses root `.venv`, `pyproject.toml`, and `uv.lock`, and starts the existing
combined worker for collection, article analysis, and review jobs. It works when
invoked by absolute path from another directory as well.

Check the selected paths without opening a database:

```sh
./traced config
```

Optional root `.env` configuration overrides `evidence-explorer/.env`; process
environment values take precedence over both. Copy `.env.example` to `.env` to
configure the root entry point. Its commented examples preserve existing settings
until you explicitly choose an override. The default store remains
`evidence-explorer/data`. Relative data paths in root `.env` or `--data-dir` resolve
from `EDTH`; existing process `EVIDENCE_DATA_DIR` and Explorer `.env` paths retain
their `evidence-explorer` base. Use absolute process paths to avoid ambiguity.
See [the root configuration guide](docs/PROJECT_ROOT.md) for precedence and commands.

Connect an existing Supabase `content_items` table through **Live sources →
Supabase content items**. Configure credentials in the private `.env`, then run
`./traced supabase check` to inspect the table before collecting. See the
[Supabase source guide](docs/SUPABASE_SOURCE.md) for column mapping and IPv4 pooler setup.

Use the selected directory in the article dashboard, without starting a second
worker. Existing databases stay in place; see the
[workspace and legacy-directory instructions](evidence-explorer/README.md#shared-review-workspace)
before selecting a different store.

### Check that you are seeing the current interface

The shared product uses the **Traced / Branch Clock** logo and the browser title
**Traced · Evidence review**. **Cases → Review → Compare → Briefing** is its shared
navigation. Explorer, Swarm Detector, and Silent Storm retain specialist screens;
their separate dashboards are not the integrated product entry point.

An already-running server does **not** reload Python or discover newly added theme
assets when files change. A responding URL or browser refresh alone does not prove
you are seeing the current build. If you see the old **ET / Evidence Trail** header,
finish active work, stop that server with `Ctrl+C` in its terminal, restart it, and
reload the page. Keep the same data-directory setting and one combined worker.

For an existing workspace in the earlier `evidence-explorer/data/evidence-trail`
location, restart from this workspace folder with:

```sh
./traced --data-dir ./evidence-explorer/data/evidence-trail start
```

Use this path only for that existing store.

A new workspace starts empty. Collect or import your sources from **Cases**.
To deliberately add labeled fictional examples to the selected workspace, run:

```sh
./traced review seed
```

The explicit demo command preserves existing articles. Normal startup and
**Import Explorer collection** never create fictional evidence automatically.

For development and shared verification:

```sh
make setup
make check
```

`make setup` installs all three applications and their development dependencies into
the root `.venv`, using the single root `uv.lock`. `make check` checks that lock,
shared identity, Ruff lint and formatting, all three test suites, and workspace-tool
regressions, then builds and verifies wheels and source distributions with a clean
installation. `make format` formats maintained code. Package names, `src/` layouts,
CLIs, and legacy launchers are preserved. Root startup shares this environment;
the older member launcher keeps its dedicated `.venv-trail` for compatibility.
Explorer's JavaScript callback regression tests require Node.js 22 (`.node-version`);
Python application startup does not require Node. For the full browser gate, run
`make browser-setup` once to install test-only Playwright/Chromium, then
`make check-all`. Standard `make check` does not install a browser. See the
[development and release guide](docs/PYTHON_DEVELOPMENT.md) for focused commands,
dependency updates, Docker builds, CI, and GitHub Release packages. These automated
checks do not establish live-provider access or replace independent human review.

For application commands and demo startup, read the
[Evidence Explorer README](evidence-explorer/README.md), the
[Evidence Trail guide](evidence-explorer/REVIEW_MVP.md), the
[Swarm Detector README](swarm-detector/README.md), and the
[Silent Storm README](operation-silent-storm/README.md).

## Alerts and evidence reports

The **Alerts** tab shows which posts about a subject have enough evidence to be
reported to X. It reads labeled posts read-only from the PostgreSQL `analytics_*`
tables written by the separate content-analytics classifier. Set
`TRACED_ANALYTICS_DATABASE_URL` in the root `.env`; the default is
`postgresql://localhost:5432/disinfo`.

- **Red alert / Ready to report:** the post has every required type of evidence:
  content (exact URL and post ID), attribution (account and user ID), preservation
  (original platform record, collection time and SHA-256), and classification at
  reporting confidence. It also has category evidence: the subject's own contradicting
  posts for false claims, a protected group for hate speech, or repeated targeting for
  harassment.
- **Needs evidence:** a strong violation with a required piece of evidence missing,
  listed per post.
- **Accounts:** accounts are reported once several of their posts are ready.
- **Download PDF evidence report:** one page per evidence item, chain of custody,
  integrity table, and preparer/reviewer sign-off. The original platform records and a
  JSON manifest are attached inside the PDF so every hash can be checked.

Thresholds and the report preparer are set under **Evidence standard** and saved in
the workspace as `alert_settings.json`. Labels are model-assisted; a reviewer
confirms each post before it is reported.

## Multiple platforms in one investigation

The shared workbench offers collection from public Telegram channels, YouTube
channel metadata, news/RSS/Atom and other public feeds under **Cases → Live sources**.
Official X and Reddit adapters expose their credential and API-access status.
Choose a starter source or add your own, collect once or enable its schedule, then
select **Open collected evidence**. Saved-source JSON import remains available.
On **Compare**, use
**Sources & timeline** to filter platforms/channels, inspect exact saved records,
and distinguish matching text, shared links, and supplied repost references.
Filters affect the browser view without excluding evidence from an assessment.

Platform account identities remain separate, unknown publication times stay unknown,
and matching content does not by itself establish coordination. Collection runs
in the combined worker; connection errors and incomplete source coverage stay
visible. YouTube descriptions are distinct from transcripts, and feed summaries
retain their source scope. Existing Explorer feed schedules and optional analysis
remain available under their current owner. See the
[multi-source workflow and input format](evidence-explorer/REVIEW_MVP.md#combine-platforms-and-news-sources)
for the 5 MiB / 2,000-item limits and steps to combine sources.

### Maltego Data MCP lookups

Each person signs in once with their own Maltego account (`./traced maltego login`),
then runs explicit lookups: `search` and `describe` find and explain capabilities,
`run` executes one on an entity and spends shared team credits. Every Data MCP call
is frozen as a hashed record in the selected workspace; `./traced maltego case`
turns records with result entities into a review case whose findings can cite an
exact record line. A record shows what Maltego returned at a time, not a verified
fact, and lookups run from the command line rather than a schedule. See the
[Maltego data source guide](docs/MALTEGO_DATA_SOURCE.md) for setup, record format,
credits, verification, and limits.

## Folder responsibilities

```text
EDTH/
├── README.md                     Workspace entry point
├── traced                        Root startup, configuration check, and review CLI
├── .env.example                  Optional root runtime configuration
├── Makefile                      Verification for all three applications
├── pyproject.toml, uv.lock       Shared uv workspace, tool policy, and locked dependencies
├── .github/workflows/           CI and checked GitHub Release packages
├── docs/
│   └── PYTHON_DEVELOPMENT.md     Setup, verification, and releases
├── evidence-explorer/
│   ├── pyproject.toml            Application metadata and dependencies
│   ├── src/evidence_explorer/
│   │   ├── cli.py, worker.py    Explorer command line and worker lifecycle
│   │   ├── config.py, models.py Configuration and validated article/claim inputs
│   │   ├── records.py           Validated storage, job, search, and collection records
│   │   ├── db.py                Compatibility facade over typed repositories
│   │   ├── persistence/         Articles, passages, claims, vectors, jobs, migrations
│   │   ├── analysis.py          Article analysis and cache orchestration
│   │   ├── models_client.py     Ollama transport and structured generation
│   │   ├── web.py               Dash factory and Explorer callbacks
│   │   ├── wsgi.py              Explicit deployment startup
│   │   ├── layout.py, ui.py     Explorer layout and presentation
│   │   └── review/
│   │       ├── contracts.py     Versioned evidence bundle and reference checks
│   │       ├── models.py        Typed review views, sessions, jobs, and scenarios
│   │       ├── storage.py       Review persistence compatibility facade
│   │       ├── persistence/     Bundle, scenario, history, job repositories/migrations
│   │       ├── service.py       Review application operations
│   │       ├── analysis.py      Evidence traversal and challenge orchestration
│   │       ├── verifier.py      Typed verifier interface and provider adapter
│   │       ├── web.py           Review app wiring and callbacks
│   │       ├── layout.py        Review component tree
│   │       ├── presentation.py  Review rendering
│   │       ├── selectors.py     Pure evidence and selection queries
│   │       └── integrations/    Swarm, media, Explorer, and package adapters
│   └── tests/                   Explorer, review, and integration regressions
├── swarm-detector/
│   ├── pyproject.toml            Specialist metadata and dependencies
│   ├── src/swarm_detector/
│   │   ├── config.py            Validated detection configuration
│   │   ├── models.py            Validated source, run, case, review, and page records
│   │   ├── detector.py          Behavioral analysis algorithm
│   │   ├── ingest.py            Normalized input validation
│   │   ├── storage.py           Compatibility facade over focused repositories
│   │   ├── persistence/         Indexed datasets, runs, cases, evidence, reviews
│   │   ├── service.py           Typed operations and bounded case/queue views
│   │   ├── web.py               Dash factory and callbacks
│   │   ├── layout.py, ui.py     Workbench layout and chart helpers
│   │   ├── presentation.py     Case, evidence, queue, and review views
│   │   └── wsgi.py              Explicit deployment startup
│   └── tests/                   Specialist analysis and application regressions
└── operation-silent-storm/
    ├── pyproject.toml            Historical-media metadata and dependencies
    ├── src/silent_storm/
    │   ├── cli.py, app.py       Specialist CLI, Dash factory, and HTTP routes
    │   ├── contracts.py         Specialist data and service interfaces
    │   ├── storage.py, ingest.py Release metadata storage and bounded import
    │   ├── reviews.py          Persistent notes, decisions, and exclusions
    │   ├── chronology.py       First-posted exercise-week projections
    │   ├── media.py            Local media references and file validation
    │   └── ui/                 Evidence inspection and review components
    ├── tests/                  Specialist unit and workflow checks
    └── dataset/                 Local source material, supplied separately
```

Working databases, exports, caches, and virtual environments belong outside `src/`.
Packaged demonstration fixtures live inside each package so wheels can include them.
The existing dataset and research paths remain stable for the importers and plans.

## Dependency boundaries

The normal path is **CLI or Dash → service → repository or analysis adapter**.
Layout and rendering helpers read supplied state. User actions reach storage through
the service. Network requests live in model/provider clients, outside transactions.

Evidence Explorer's adapters may depend on Swarm Detector's public analysis and
service interfaces. Swarm Detector remains independently runnable and does not
depend on Explorer or its review UI. Silent Storm owns its supplied-media methods
and specialist review state; the bounded media adapter and validated evidence bundle
provide the handoff into the shared workspace. Track that one-way handoff in the
shared plan; automatic two-way review synchronization is outside v1.

Explorer's and Swarm's `web.py`, and Silent Storm's `app.py`, expose factories without
constructing an application on import.
Gunicorn deployments use `evidence_explorer.wsgi:server` or
`swarm_detector.wsgi:server`; those explicit entry points initialize the applications.
The Explorer WSGI/container path serves standalone Explorer. The integrated review
desk uses the launcher above; its local launch is not a public deployment claim.

When extending the project, add validation at an input boundary, domain logic in its
own module, service orchestration, and rendering in that order. Add regression tests
for observable behavior and failure cases. Avoid a shared helper package until two
applications actually need the same contract and behavior.

## Persistence and memory guarantees

Writable startup applies versioned migrations while preserving saved investigations.
Explorer and Review have separate migration histories in their shared SQLite file;
Swarm has its own database and migration history. Silent Storm keeps its release
metadata and specialist reviews in its separate local database while media stays
on disk. Legacy read-only Explorer snapshots remain readable without being upgraded
in place. Selecting the integrated workspace does not merge these specialist stores.

Interactive collection, queue, case, and source reads use metadata queries and bounded
pages. Explorer search streams vector batches rather than building a corpus-sized
matrix. Full analysis, legacy projection backfills, snapshots, and complete exports
remain explicit materialization paths.

See the [Swarm persistence guide](swarm-detector/docs/PERSISTENCE.md) for pagination,
revision pinning, migration guarantees, and limits.
