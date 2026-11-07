# Evidence Trail

Evidence Trail is the shared review workspace for checking investigation findings before using them in a briefing. It brings saved Explorer claims, Swarm behavioral results, and historical media observations into the same evidence, challenge, and decision flow.

The included examples are labeled fictional cases.

## Start the workspace

The common project entry point is `./traced start` from the parent `EDTH` directory.
It uses root configuration and the shared development environment; see
[the root guide](../docs/PROJECT_ROOT.md). The older member launcher remains available
with its existing configuration. From the `evidence-explorer` directory, run:

```bash
./scripts/start-trail.sh
```

Open [http://127.0.0.1:8053](http://127.0.0.1:8053). The launcher starts the review desk and its combined background worker. It uses a dedicated environment, `.venv-trail`, and the same data-directory configuration as Explorer: `EVIDENCE_DATA_DIR` from the process environment or project `.env`, with `./data` as the default. Process environment values override `.env`. The database is `evidence.sqlite3` inside the selected directory; saved media lives alongside it in `trail-artifacts`.

The launcher changes into the `evidence-explorer` project directory before starting the application, including when invoked by absolute path from another directory. Relative data paths therefore resolve from that project directory. Run the manual commands below from the same directory, using the same environment or `.env` setting for the collection dashboard and Trail.

You need Python 3.12 and `uv`. The first setup installs application dependencies, including the local Swarm package. No model download or cloud account is needed to use the sample cases.

The equivalent manual command is:

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/.venv-trail" uv run --locked --extra swarm \
  python -m evidence_explorer.review.integrations \
  web --with-worker --port 8053
```

Choose another `--port` if 8053 is occupied. Keep the terminal open; press Control-C to stop the application.

Earlier launcher versions selected `data/evidence-trail`. Existing cases in that directory remain available by explicitly selecting it:

```bash
EVIDENCE_DATA_DIR=./data/evidence-trail ./scripts/start-trail.sh
```

Use this same setting for the collection dashboard if you want it to share that store. Selecting a directory opens its saved state in place; no database or evidence files are moved or merged. To keep this choice for subsequent commands, set `EVIDENCE_DATA_DIR=./data/evidence-trail` in the project `.env`.

A fresh workspace starts empty. Collect a public source or import a case package to begin. To explicitly load the fictional demonstration cases into the selected workspace, run:

```bash
.venv-trail/bin/python -m evidence_explorer.review.integrations seed
```

The ordinary web launch and **Import Explorer collection** do not create fictional articles when the source collection is empty. The seed command is the separate demonstration action.

## Try the review flow

1. Choose a case and a finding. Read its statement and whether it is an observation, inference, or assumption.
2. Open a linked source. Inspect the exact saved passage, message context, metadata, or supplied image.
3. Inspect repeated content and source relationships. Identical content, documented reposts, and similar wording have separate meanings; unknown independence stays unknown.
4. Add a reason and select **Exclude from assessment**. The workspace creates a reversible scenario and shows which findings need review.
5. For a Swarm case, **Inspect strongest episode** opens its parent discussion. Exclude that source to select the full documented episode family, then select **Recompute Swarm scenario**. Wait for the completed result and select **Review computed result**.
6. Record **use**, **qualify**, or **withhold**, with your reason. Each decision belongs to the exact finding and assessment being reviewed.
7. Download **Briefing**, **Evidence JSON**, or **Case package**. Use **Return to original** to inspect the baseline again; the scenario and review history are retained.

The available examples cover:

| Example | What to inspect |
| --- | --- |
| Harbor investigation | Repeated posts and an attribution that depends on an ambiguous address match |
| Swarm demonstration | Actual detector output from fictional comments, including background activity and transparent volunteer coordination |
| Historical media demonstration | Harmless synthetic image cards with frame, clip, video, and exercise-week metadata |
| Explorer snapshot | Saved fictional articles, passages, and extracted claims from the existing starter |

The historical-media demonstration uses synthetic cards rather than conflict footage. Real-media imports are bounded selected subsets; a supplied frame opens locally. An exercise week means first-posted exercise week, not capture or event time. Posting-channel affiliation does not identify the side depicted. Missing original URLs, dates, locations, and event identity remain unknown.

## Understand exclusions and recomputation

An exclusion initially creates a **dependency preview**. It shows review impact while preserving the original evidence. It does not establish that a conclusion is false or that the detector has recalculated its result.

A completed Swarm replay is different: it calls the existing detector using the complete declared input subset, surrounding activity, recorded settings, and random seed. Excluding a supplied parent post removes its documented post family and associated comments. Excluding an individual comment removes that comment only.

If a case lacks complete analysis inputs, inspection still works and recomputation is unavailable. New results keep their own finding snapshots and review decisions. The comparison identifies continuations, splits, merges, additions, removals, and uncertain matches rather than silently transferring approval.

For historical media and ordinary text claims, the shared dependency view identifies claims needing reconsideration. A source exclusion does not automatically determine a historical date, location, or event identity.

## Import and share a case

Use **Import case** to open a saved Evidence Trail ZIP package or evidence JSON. A package includes the saved evidence files, finding snapshots, relevant runs, and review history required to inspect the exported assessment on another workspace.

Choose **Case package** when the case includes local images. JSON alone does not carry those files. Package imports validate evidence references, content hashes, and relative file paths. Importing an unchanged package again preserves the existing records and imported history.

On **Brief**, the **Draft dossier** panel identifies the selected baseline or scenario, its version and run, saved decisions, omissions, evidence availability, and unresolved gaps. Expand **Omitted statements** to return to the exact review without changing assessment. An evidence change can require review even when no earlier decision exists. The panel uses saved case scope; it never substitutes the collector's current contents.

**Download draft dossier** prepares Markdown with the current saved brief and an explicit checklist of gaps. Missing media can be described in a draft dossier but prevents downloading a complete case package. Unsaved review notes block all downloads until they match a saved decision. The selected purpose is only session guidance, not a confirmed investigation brief. Platform references carry their recorded dates and unknown applicability; downloading does not submit a report or record recipient acceptance.

Each download uses a consistent saved snapshot. If the evidence, assessment version, or saved review changes before export validation, refresh the assessment and retry; a later change cannot silently enter a file already being prepared.

The handoff also preserves relevant investigation-planning revisions and attributed confirmation history in the **case package**. Expand **Planning history** to inspect exact revision IDs, authors, dates, and saved scope bindings. Imported confirmations remain historical attribution; reopening a package does not select an active local plan or grant local approval. Packages carrying planning history require a reader that supports it; older evidence-only readers reject these packages rather than silently dropping the plans.

**Evidence JSON** contains native evidence and review history only. It omits investigation-planning history and local media; use the case package for the complete handoff. The draft dossier includes a readable planning-history section. A changed planning revision invalidates an older handoff preview: choose **Refresh handoff**, inspect the revised preview, then download. Unsaved planning edits block downloads for their matching case until saved or discarded; unrelated drafts are never added to the package.

After import, a receipt lists validated records and exact references, saved runs, assessments, reviews, and local media availability. It distinguishes retained Swarm recomputation context from a completed rerun or locally verified runtime support. Case-level synthetic labels never override individual record provenance in the interface, brief, or dossier.

Use **Import current Explorer** to capture the articles and claims currently saved in this workspace's Explorer database. The imported case is an immutable snapshot. Later changes to the article corpus produce a separate review snapshot rather than changing an earlier assessment.

After the launcher has prepared `.venv-trail`, the same actions are available from the command line. Run these from `evidence-explorer`; they use the same `EVIDENCE_DATA_DIR` or project `.env` configuration and `./data` default:

```bash
.venv-trail/bin/python -m evidence_explorer.review.integrations seed

.venv-trail/bin/python -m evidence_explorer.review.integrations cases

.venv-trail/bin/python -m evidence_explorer.review.integrations explorer

.venv-trail/bin/python -m evidence_explorer.review.integrations \
  import /absolute/path/to/case.zip

.venv-trail/bin/python -m evidence_explorer.review.integrations \
  export CASE_ID /absolute/path/to/new-case.zip
```

Copy a case ID from `cases`. Choose a new export filename; existing files are not overwritten. The import action also accepts a JSON evidence bundle.

For a one-off directory selection in a manual command, put the global `--data-dir` before the action, for example `.venv-trail/bin/python -m evidence_explorer.review.integrations --data-dir data/evidence-trail cases`. The launcher accepts web options such as `--port`; select its data directory through `EVIDENCE_DATA_DIR` or `.env`.

## Background processing and optional local models

The launcher includes a combined worker for article collection and analysis, cited review checks, and Swarm recomputation. Run only that combined worker for the selected data directory when using both dashboards. Start the collection dashboard with `uv run evidence-explorer web --port 8051` and the same data-directory configuration, without starting the original Explorer worker. The two interfaces then use the same article and review tables and the same worker lock.

If you start the web application without `--with-worker`, run the combined worker in a second terminal:

```bash
.venv-trail/bin/python -m evidence_explorer.review.integrations worker
```

Add `--once` to drain queued work and exit. Do not also run the original Explorer worker against the same directory.

The labeled demonstration challenge works offline. **Ollama · local model** is optional and reuses the starter's configured local chat model and client. See the [local-model setup](README.md#use-local-models) before selecting it. A missing server or model produces a visible job error rather than substituting demonstration results. Model suggestions cite saved evidence and require human review; they do not assign missing historical facts or overwrite decisions.

## Reused components and boundaries

| Component | Existing behavior reused |
| --- | --- |
| Evidence Explorer | SQLite repository, article/claim pipeline, job processing, configuration, model client, and worker lock |
| Evidence Trail review package | Versioned evidence contract, immutable findings, review storage, dependency inspection, shared Dash interface, and cited challenges |
| Swarm Detector adapter | Unchanged behavioral detector and fictional dataset; full input context is retained for valid scenarios |
| Historical-media adapter | Bounded local-file import with frame/clip/video relationships and explicit unknown metadata |
| Portable packages | Selected evidence files, hashes, finding snapshots, analysis provenance, and review history |

Each producer retains its specialist meaning. A behavioral candidate does not establish automation or intent; repeated media does not establish independent corroboration or an event count.

The dedicated launcher environment prevents another active EDTH chat's dependency synchronization from changing the interpreter serving this workspace. Keep `.venv-trail` and local data outside version control. The standalone Explorer and Swarm applications remain independently runnable.

## Verify changes

From `evidence-explorer`, with the launcher environment prepared:

```bash
.venv-trail/bin/python -m pytest
.venv-trail/bin/ruff check .
.venv-trail/bin/ruff format --check .
```

The meaningful integration checks cover exact evidence references, import idempotency, preserved unknowns, complete-context Swarm replay, stale-job rejection, distinct baseline/result decisions, and portable export/import. They use fictional fixtures and mocked model responses; no live model inference is required.

## Troubleshooting

- **The browser cannot connect:** confirm the launcher terminal is still running and use its displayed port.
- **Work stays queued:** start the combined worker with the same data directory, or restart with `--with-worker`.
- **Another worker holds the lock:** stop the other worker for that directory before starting another.
- **A result says the scenario changed:** review the current exclusions and submit a fresh operation. A result for an older scenario is not applied automatically.
- **A media file is missing or its hash changed:** reimport the intact case package. Do not substitute a different file under the same evidence identity.
- **Swarm recomputation is unavailable:** the case needs the full declared normalized input, including background activity. A selected group of comments is insufficient.

## Check a claim against external evidence

On **Review**, select a finding and expand **Check claim with external evidence**.
The default search uses DuckDuckGo's public non-JavaScript Lite page; no API key is
required. Queries leave this machine for search, while fetched passages are
assessed by the configured local Ollama model. The public search page can change,
rate-limit, or reject requests; such failures remain visible and are never
replaced by demonstration results.

1. Optionally enter up to three search queries and six article URLs. Leave queries
   blank to search the selected claim and a correction/evidence variant. Choose
   a collection budget from one to eight attempted sources (default six).
2. Select **Check claim**. The shared worker saves search attempts, fetches actual
   publisher pages, groups exact copies, and freezes an evidence snapshot before
   inference. Snippets remain discovery leads and never become article evidence.
3. Inspect progress and saved checks. Collection alternates among seed URLs and
   each query so early results cannot consume every slot. Failed downloads and
   unfetched URLs remain explicit coverage issues.
4. Open the completed result. The local model proposes **supported by reviewed
   evidence**, **contradicted**, **misleading context**, **insufficient evidence**,
   or **not factual**. It selects supplied passage IDs; exact quotations come
   from saved source text. A second local pass produces cited candidate objections.
   Neither pass establishes source independence, coordination, or deceptive intent.
5. Inspect the original claim context and fetched records. Save **Use**, **Qualify**,
   or **Withhold** with a reason, then export the case package. The proposal,
   search coverage, exact passages, model provenance, and analyst review survive
   export and reopening. An originating test claim remains distinguished from
   real collected articles.

Every result is a separate immutable case. The source investigation and its
reviews are preserved. A changed source finding/case invalidates an in-flight
check; a completed historical check is marked stale in its source history.
**Retry failed check** reuses frozen evidence when available. Submit a fresh check
when you want new searches. Empty evidence completes with **insufficient evidence**
without calling the model; invalid citations fail visibly. Exclusions in an opened
result still use the normal reversible review workflow and require a new human decision.

The same operations are available through the CLI:

```bash
.venv-trail/bin/python -m evidence_explorer.claim_check \
  --data-dir data/evidence-trail submit CASE_ID FINDING_ID \
  --query 'a specific factual claim' --max-sources 6

.venv-trail/bin/python -m evidence_explorer.claim_check \
  --data-dir data/evidence-trail show JOB_ID
```

Use the existing combined workspace worker. If none is running, this drains
pending work through the same worker lock:

```bash
.venv-trail/bin/python -m evidence_explorer.claim_check \
  --data-dir data/evidence-trail run
```

Do not start competing workers against one
data directory. The local chat model comes from `EVIDENCE_CHAT_MODEL`; there is no
cloud or demo fallback for an external claim check.

Implementation is isolated in `claim_check/`: contracts, discovery, collection,
passage assessment, durable storage, service/workflow, result-bundle adaptation,
CLI, and UI. Existing collection safeguards, Ollama transport, review decisions,
and portable-package handling are reused. This first version checks text claims;
it does not authenticate image/video content or establish disinformation intent.
