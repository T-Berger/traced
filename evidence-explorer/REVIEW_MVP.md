# Evidence Trail review MVP

The source-neutral review module lives inside the existing Evidence Explorer application at `src/evidence_explorer/review/`. It adds its own `review_*` SQLite tables and can share the starter's database. Its command and web screen are independently runnable while the broader application is integrated.

## Shared readable workflow

For the integrated application with agent investigations, Swarm findings, and historical media,
run from the `evidence-explorer` directory:

```bash
./scripts/start-trail.sh --port 8053
```

The launcher uses its dedicated `.venv-trail` environment and the same configured
workspace as Explorer (`EVIDENCE_DATA_DIR` from the environment or `.env`, default
`./data`). See [EVIDENCE_TRAIL.md](EVIDENCE_TRAIL.md) for the shared setup and worker
lifecycle. Existing data directories are not moved or merged.

The shared interface has four destinations: **Cases → Review → Compare → Briefing**,
with the existing route IDs. Follow **Plan investigation → Gather evidence → Confirm
scope → Investigate findings → Review conclusions → Prepare and share briefing**.

On Cases, **Start investigation** opens the existing **Investigation plan** editor
without writing saved state or clearing a working draft. Choose the primary purpose
in the plan and select **Save plan**. **Explore workflows** is optional guidance;
its session-local choice does not replace the saved purpose or confirm scope.
Expand **Gather evidence**, collect or import evidence, and save a case. Return to
**Investigation plan**, choose the draft under **Continue a saved plan**, and select
**Open saved plan**. Select **Use this case's sources in the plan**, **Save plan**,
**Attach collected evidence**, and **Confirm scope** as separate explicit actions.
Imported cases use the same attachment action. **Start another plan** is secondary;
use it when you want a separate plan. Earlier versions and imported-plan adoption
are under **Plan versions and imported history**.
See [the planning guide](../docs/INVESTIGATION_PLANNING.md) for revisions and recovery.

Inspect findings and their exact saved evidence, save a Use / Qualify / Withhold
decision with a reason, and preview the selected assessment on Briefing before
downloading. Collection preserves source records; it does not establish a substantive
claim assessment. Optional analysis, charts, source relationships, technical settings,
and decision history are expandable. Missing model or specialist inputs leave manual
evidence inspection available.
Body text is 16px; the layout supports light/dark appearance and stacks on narrow screens.

Synthetic notices describe the **selected case**. Separately collected Live sources
keep their own provenance, and opening a real collected case clears the notice.
Test-claim cases distinguish the originating claim from their saved external evidence;
source origin and saved decisions remain intact in brief and package exports.

Baseline decisions use their own durable assessment context. Testing without a record
creates a separate scenario; switching back to Baseline preserves its decisions.
Exclusion and restoration create assessment versions that require their own reviews.
Compare distinguishes dependency impact from a completed detector recomputation.

Review notes remain drafts until a decision is saved. Drafts survive finding changes
and browser refresh in the same browser session. Downloads are disabled when the
selected assessment has unsaved notes. The Briefing preview and Markdown download use
only current decisions from the selected assessment, and explain omitted statements.
The ZIP contains saved evidence, media, all analysis runs, and historical assessments.
Reimport a ZIP on Cases to inspect its exact saved review contexts without rerunning models.

The reusable screen sections live in `review/components.py`; specialist import, media,
comparison, and export controls fill explicit slots via `review/integrations/components.py`.
`controller.py` owns shared action transitions, `drafts.py` owns draft identity and export
checks, and `workspace_views.py` renders the four destinations from supplied state.
`workspace_ui.py` binds navigation, assessment context, draft hydration, and brief previews.
`workflow.py` supplies the same saved-decision projection to the screen and brief exporter.

### Readable evidence graphs

“Explore evidence connections” opens a full-width diagram of the selected finding
and up to four direct evidence records or premise findings. The summary gives the
shown and total counts; the complete relationship list retains every direct
connection. Repeated citations share a line while preserving their individual
link and passage references. Arrows run from evidence/premises toward the selected
finding; “premise for” is the display label for a declared dependency.

Select a record to open its evidence, or select a connection to inspect its stated
basis and exact passages. “Fit graph” restores a useful reading frame after zooming.
Narrow screens use the keyboard-accessible connection list with full labels.
Excluded records retain readable labels and a dashed border; contradiction links
have their own label and styling. Neither graph position nor line count is a truth
or independence score.

The pure graph projection lives in `review/graph.py`; graph detail, list, and fitting
callbacks live in `review/graph_ui.py` and reuse the shared evidence inspector.

The original text-only review entry point below remains available for isolated development.

## Compare two saved sources

On **Compare**, choose two text records and select **Compare sources**. The source
comparison is available in both the standalone review desk and the integrated
workspace. It uses saved evidence and works without a model or an internet connection.
Changing the case, selected sources, or review context clears the previous result;
select **Compare sources** again to inspect the current pair.

The two source panels retain the original wording and distinguish publication time
from acquisition time. Possible conflicts show both exact passages and the reason
they were flagged. Closely matched sentences can flag changed numbers or added or
removed negation; wording, entities, units, and context still need human review.
Publication time is never substituted for the event time described in a claim.

These are candidates for inspection, not decisions about which report is true.
Different units are not converted, source independence is not inferred, and a pair
with no flags is not proof of agreement. Coverage notices identify text omitted by
the bounded comparison. Comparing sources does not change findings, saved decisions,
or scenario exclusions; the existing assessment comparison remains available below.

For a reproducible demonstration, import
`src/evidence_explorer/review/data/source-comparison-example.json` through **Cases**.
Choose the fictional Rivergate council bulletin and Rivergate news report. Their
otherwise comparable statements differ in the evacuation count and bridge-closure
wording. The other supplied records demonstrate a different place and a different
counting unit. All four records are explicitly synthetic.

The pure projection lives in `review/comparison.py`; application operations are
exposed by `ReviewService`, and `review/comparison_ui.py` supplies the shared controls
and callbacks. Comparison styling is isolated in `review/assets/comparison.css`.

## Combine platforms and news sources

### Collect directly from public sources

On **Cases**, expand **Gather evidence**. Under **Live sources**, choose a starter
channel or enter a custom source.
Select **Add & collect** to queue a bounded background fetch. Each saved source
shows its actual collection status, saved-record count, last successful fetch,
and any access or network error. No file import is required.

Select **Choose sources for a case**, pick the saved sources to include, then
select **Preview selected sources**. The preview lists each source's saved and
included record counts, unknown publication times, latest collection state and
errors. Access readiness is shown separately from collection history. The types
of saved content come from the included records' provenance, so a failed refresh
cannot erase the feed-summary or video-description label.
Selected sources with no records remain visible, including failed or
uncollected sources. At least one selected source must have saved evidence before
the case can be created.

Select **Create case from selected sources** to save the exact previewed scope and
open it in Review. A refresh or source change after preview requires another
preview. This step reads saved material only; it does not fetch sources or call a
model. Unselected sources are absent from the case. Source selection defines a
new case and is separate from analytical exclusions in an existing assessment.
Return to **Investigation plan**, choose your draft under **Continue a saved plan**,
and select **Open saved plan**. Select **Use this case's sources in the plan**,
**Save plan**, **Attach collected evidence**, then **Confirm scope**. Creating the
case does not confirm the plan or approve its findings.
The saved scope and its gaps remain visible in Review and survive case export and
reopening. Repeating the unchanged selection and evidence snapshot reopens the
same case; changed scope or coverage produces a separate immutable case.

Each source retains the text, acquisition details and observation order it
actually collected. A refresh from an unselected source cannot replace them.
For a native item observed differently by multiple selected sources, the case
uses one newest saved observation, with source ID as the tie-breaker. The preview
and saved scope identify omitted versions by source, observation time and saved
payload hash, and explain the rule. These versions do not establish independent
corroboration. Payload hashes and saved-text hashes are distinguished; a metadata
difference does not prove that the text changed. Omitted content is not included
as readable evidence in the case package. Equal capture times use the disclosed
source-ID convention, which does not establish the newest platform edit.
Every selected observation is fingerprinted and listed in the saved manifest,
including versions omitted by that rule or the case limit, so changing one
requires a fresh preview.

Collection schema version 3 adds nullable source-specific observations to existing
source/item associations. Earlier associations whose exact source history is
unknown stay unattributed and are omitted from new source-selected cases until
that source is collected again. Their count and explanation remain visible;
global saved items and existing case packages are preserved without inventing
historical attribution.

Select **Keep collecting on this schedule** for automatic refresh, or use
**Collect now**. The interval is 5–10,080 minutes and each run collects 1–20 items.
Scheduled collection refreshes source material. It does not evaluate activity rules
or send alerts. The optional [manual activity comparison](../docs/ACTIVITY_REVIEW.md)
requires compatible saved windows and an immutable coverage declaration; successful
collection alone is insufficient. No live alert service or notifications are activated.
The integrated worker must remain running (`web --with-worker`, or its separate
`worker` command). Pause affects scheduled work; an explicit manual collection is
still available. API rate-limit deadlines also apply to manual retries.

| Source | Collected material | Access |
|---|---|---|
| Telegram | Recent text/captions exposed in a public channel preview | Public channels; private or restricted posts are unavailable |
| YouTube | Recent video titles, descriptions, publication dates, and links | Public channel ID or `/channel/…` URL; video transcripts are separate |
| News/RSS/Atom | Text supplied by a feed, or public articles linked by it | Choose the content mode; summaries retain that label |
| X | Posts from an account through its official API | Server-side `X_BEARER_TOKEN` with endpoint access |
| Reddit | Community posts through its official API | Approved `REDDIT_ACCESS_TOKEN`, or registered `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET`; `REDDIT_USER_AGENT` is optional |

Starter sources include Telegram announcements/Tips/Durov; NASA, BBC News and DW
video channels; BBC World, DW, Tagesschau, Guardian World, NASA, UN News and
Deutschlandfunk feeds; Mastodon announcements and Hacker News; and X/Reddit
presets that remain setup-needed until credentials are configured. A preset is
not evidence of connectivity: successful fetches establish live status.

Credentials belong in the server environment or local `.env`, never in source
forms, saved records, case packages, or this chat. Account sign-in, platform
approval and any required terms are separate setup steps. Missing or denied
access, rate limits, empty previews and failed downloads remain visible; no
sample evidence replaces a failed collection.

The collection package separates contracts, platform adapters, canonical locator
validation, subscriptions/jobs/items, scheduling, and application operations.
`integrations/collection_ui.py` only renders those operations in the shared UI.
Collection joins the combined worker under its existing lock. Existing RSS/social
feed schedules keep their owner; the same canonical source must not acquire a
second automatic poller in another screen.

Saved text, original links, acquisition method, content scope and available dates
survive evidence export/reopen. Later refreshes update the collected-source store;
opening it creates a new immutable review snapshot when the evidence changed.
Earlier snapshots and decisions remain available. Unknown source dates remain
unknown, and feed summaries/video descriptions do not become full articles or
transcripts.

### Import already saved material

The shared workbench can import saved Telegram, X, Reddit, YouTube, news, and other
text records into one investigation. Start the integrated application on port 8053
with the command above, then:

1. On **Cases**, expand **Import saved sources** and select **Download source example**.
2. Replace the fictional records with your saved material, then upload the JSON or
   use **Paste source JSON instead**. Set `case.synthetic` accurately.
3. Inspect the preview after uploading, or select **Validate pasted sources** after
   pasting. Check the case title, counts, and limitations. Validation does not save a
   case or call a model. Editing the pasted text requires validation again.
4. Select **Import sources** to save and open the investigation.
5. On **Compare**, use **Sources & timeline** to filter by platform or channel and
   open a saved record in the evidence inspector. Filters change this browser view;
   they do not exclude records from the assessment.

The input is a UTF-8 JSON object with `source_schema: "1.0"`, a `case` object
(`title`, `question`, `synthetic`), and an `items` array. Each item contains:

| Field | Meaning |
|---|---|
| `platform` | `telegram`, `x`, `reddit`, `youtube`, `news`, or `other` |
| `channel_id`, `channel_name` | Source-native channel or publication identifier and readable label |
| `native_id` | The item's original identifier within that platform and channel |
| `text` | Exact saved text; keep the original wording |
| `acquired_at` | Required acquisition time for this saved material, with an explicit timezone |
| `published_at` | Optional original publication time with a timezone; omit or use `null` when unknown |
| `source_url`, `author_id`, `title` | Optional source link, opaque author identifier, and title |
| `shared_urls` | Optional array of links contained in the saved material |
| `repost_of` | Optional explicit source reference: `{ "platform": "…", "channel_id": "…", "native_id": "…" }` |

Imports are limited to 5 MiB and 2,000 items. Platform, channel, and native item IDs
form the source identity, so identical identifiers on different platforms stay
separate. Account identifiers are retained without merging accounts across platforms.
Unknown publication times remain unknown; acquisition time is not substituted.
Identical reimports reopen the same saved snapshot. Changed input creates a new
snapshot case, preserving the earlier case and its review history.

The comparison separates exact saved text, shared links, and explicitly supplied
repost references. Sharing a URL does not establish identical content, authorship,
coordination, or the direction of spread. A repost reference records supplied
provenance; it is not independently verified by the importer. Swarm's behavioral
analysis remains a separate method.

This optional importer reads normalized saved records rather than raw platform
exports. Use **Gather evidence → Live sources** above for direct collection. Explorer's **Add news**
and saved-feed workflow also remain available, with optional local analysis;
**Import Explorer collection** opens a snapshot of that saved article collection.

The same source file can be imported from the command line:

```bash
uv run --locked python -m evidence_explorer.review.integrations import-sources /absolute/path/to/sources.json
```

## Run the working demo

From the `evidence-explorer` directory:

```bash
make -C .. setup
uv run --locked python -m evidence_explorer.review web --port 8052 --with-worker
```

Open http://127.0.0.1:8052/. The first launch seeds the explicitly synthetic Harbor investigation if the review store is empty. The embedded worker is convenient for local development; debug/reload is disabled to avoid starting duplicate workers.

The fixture contains ten saved records, exact text fragments, three findings, and typed dependency/repost relationships. It includes a deliberately unsupported attribution to a fictional company. The original agent's search history is unavailable and original source URLs stay null.

Try this flow:

1. Select the company attribution and inspect the saved domain/company passages.
2. Enter an exclusion reason and exclude the domain record. Attribution needs review; the saved posting observations remain available.
3. Restore the record. The scenario returns to its baseline evidence state.
4. Open source relationships. Inspect exact text groups separately from documented repost chains.
5. Run a demo challenge, inspect its cited source, and save a use/qualify/withhold decision with a reason.
6. Refresh the browser: the selected scenario is restored from session IDs and its data is loaded from SQLite.

Demo challenges are deterministic fixture checks, visibly labelled. They are never substituted for real model responses.

## Import an investigation from the screen

Open **Cases → Advanced: validate investigation JSON**, choose a `.json` file or paste its contents, and inspect the preview. The preview shows the investigation question, record/fragment/finding counts, declared limitations, and whether the case is synthetic. Select **Import and open** to save it in SQLite and open its review screen. Uploading and validating alone do not save a case or invoke a model.

The importer accepts UTF-8 evidence bundles up to 5 MiB in the shared v1 format. Download **example JSON** in the screen for a complete, validated starting point. That example has two fictional sources and a deliberately unsupported finding; replace them with your investigation and set `case.synthetic` accurately. This is a structured investigation importer, not a raw CSV/article extractor.

- Keep original saved text in `records[].text`; `content_hash` is the hexadecimal SHA-256 of that exact UTF-8 text.
- Store cited spans in `fragments`, with exact quotes and character offsets (`start` inclusive, `end` exclusive).
- Include the producing `run` and its declared input record IDs; use `null` for unknown source URLs or timestamps.
- Give each finding an immutable `snapshot_id`, `lineage_id`, `version`, and `run_id`; state assumptions explicitly.
- Add evidence links from the source record/fragment or premise finding **to** the conclusion. The example demonstrates the shape.

Validation rejects incorrect hashes/quotes, missing references, dependency cycles, duplicate JSON keys, unsupported schema versions, and conflicting immutable IDs. Reimporting the same objects is safe; additional objects extend a saved investigation. An import with no findings can be saved, but it needs an assessment before a challenge can run. Media artifacts still use the specialist package importer.

## Run a real local model check

The first live provider is **Ollama**. The existing defaults are `http://127.0.0.1:11434` and `qwen3.5:9b`; no cloud key is required. Start Ollama with that model installed, then start the review app with `--with-worker` (or start its separate worker command). Configure another installed model with `EVIDENCE_CHAT_MODEL` and restart both app and worker.

1. Open an imported investigation and select a finding.
2. Choose **Ollama · local model** in the evidence check section and use **Check connection** if needed.
3. Run the check. The screen polls the durable job while the worker calls the local model.
4. Inspect each objection's exact quote and cited record. A candidate objection is not an automatic analyst decision.
5. Save a use/qualify/withhold decision with your own reason.

The request uses a bounded selection of saved passages and excludes sources removed in the selected scenario. Jobs pin the finding, evidence fingerprint, scenario version, provider, model identity, and prompt version. Changed local model weights or stale case inputs require a fresh check. Invalid or unsupplied citations fail visibly; the system does not repair them by inventing evidence. Source text is untrusted data inside the model prompt.

Ollama's structured JSON response is validated again locally, including exact quotations. Incomplete generations and mismatched reported model identities are rejected. See the [Ollama structured-output API](https://docs.ollama.com/capabilities/structured-outputs) for the underlying response format.

## Commands and configuration

```bash
uv run --locked python -m evidence_explorer.review seed
uv run --locked python -m evidence_explorer.review import path/to/bundle.json
uv run --locked python -m evidence_explorer.review worker
uv run --locked python -m evidence_explorer.review worker --once
uv run --locked python -m evidence_explorer.review --db /absolute/path/review.sqlite3 web --port 8052
```

The default database is `data/evidence.sqlite3`. Set `EVIDENCE_REVIEW_DB` or pass `--db` before the subcommand. A provider's `PORT` environment variable supplies the default web port. The web server binds to localhost unless `--host` is supplied.

OpenRouter remains an optional cloud provider in the challenge selector. Set `OPENROUTER_API_KEY` and `EVIDENCE_REVIEW_MODEL` in the server environment or project `.env`, then restart the app and worker. Select a model supporting [OpenRouter structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs). Credentials never enter browser state or saved jobs. A check sends the selected finding and bounded passages to OpenRouter only when that provider is selected. Missing configuration, request failure, and invalid citations fail visibly; there is no automatic provider or demo fallback. Retrieval currently uses bounded lexical overlap plus directly linked passages within the imported case. It is not an internet-wide search or an exhaustive contradiction check.

Run a standalone worker alongside the web process when integrating into a host's process supervisor. Persist the database directory on the same machine/container; a separate hosted service cannot share an instance-local SQLite disk. Public hosting and automatic specialist recomputation are outside this initial slice.

## Modules and ownership

| Module | Responsibility |
|---|---|
| `contracts.py` | Versioned common bundle; saved hashes, typed references, exact selectors, run references, acyclic dependencies |
| `data/harbor.json`, `demo.py` | Labelled fictional fixture and package loader |
| `importing.py`, `import_ui.py`, `data/import-example.json` | Bounded parsing, validation/preview/commit, upload/paste controls, and editable example |
| `integrations/sources.py`, `integrations/sources_ui.py` | Saved multi-platform source imports, source identity, comparison groups, filters, and timeline |
| `collection/`, `integrations/collection_ui.py` | Modular live platform adapters, source readiness, subscriptions, durable collection jobs, collected items, and shared UI controls |
| `provider_ui.py` | Local/cloud provider readiness, setup guidance, and processing disclosure |
| `storage.py` | Additive immutable-ID import, scenarios, audit events, decisions, version-pinned jobs and leases |
| `analysis.py`, `verifier.py` | Dependency impact, exact-text and documented-lineage grouping, scoped challenge checks, citation validation |
| `service.py` | Application operations used by UI and CLI |
| `worker.py` | Background execution and errors, with protection against stale worker attempts |
| `web.py`, `layout.py`, `presentation.py`, `selectors.py`, `assets/review.css` | Dash callbacks, screen construction, component rendering, selection queries, and styling |
| `__main__.py` | Isolated commands that do not replace the starter CLI |

## Bundle semantics

The v1 envelope follows the shared plan: `case`, `records`, `fragments`, `runs`, `findings`, `links`, `reviews`, `scenarios`. Finding references use immutable `snapshot_id`; `lineage_id` connects successive versions. Additive producer metadata is preserved.

- `supports` and `depends_on` point **from premise to conclusion**.
- `documented_repost` points **from the original record to the repost**.
- Reusing an immutable object ID with changed content is rejected. Identical reimports are idempotent; additive objects can extend a case. Case metadata is immutable in this slice.
- Text offsets count Python Unicode characters in the saved text, with an exclusive end offset. A quoted span must match exactly.
- Exact textual duplicates and documented lineage are separate. Neither establishes independent origin by itself.
- Exclusions are scenario state. They flag dependencies and preserve remaining evidence; they do not establish falsity or rerun a specialist detector.
- Decisions check the scenario version and evidence fingerprint the reviewer saw; stale submissions are rejected. Previous decisions remain in history.
- Jobs pin the bundle fingerprint, finding snapshot, scenario version and excluded IDs. Old results are marked stale after relevant state changes.
- The current importer accepts text bundles. Media selectors are represented by the contract, but the service rejects artifact-bearing bundles until a verified artifact loader is integrated.
- Named reviewer identity currently defaults to `Analyst`; this is a local review demonstration, without an authentication system.

## Validation

```bash
uv run --locked --all-packages --all-extras --group dev pytest tests/review -q
uv run --locked ruff check src/evidence_explorer/review tests/review
uv run --locked ruff format --check src/evidence_explorer/review tests/review
```

Tests cover invalid references and quotations, changed hashes, cycles, additive import, case/scenario isolation, optimistic version conflicts, event persistence, worker recovery, stale attempts, alternative evidence, explicit lineage, bounded provider requests, citation mismatch, and the integrated review flow after a database restart.
