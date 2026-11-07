# Swarm Detector

A local analyst workbench for reviewing repeated coordination in Telegram comments.
It separates behavioural evidence from narrative annotations and preserves the comments
behind each connection. A candidate group is not a finding that its members are bots,
inauthentic, foreign-controlled, or operated by a particular organisation.

The current stack is **Python 3.12, Dash, Dash Cytoscape, Plotly, NetworkX, and SQLite
with FTS5**, managed with uv. Docker and Gunicorn provide an optional serving path.

## Current sample workflow

1. **Find:** inspect dataset readiness and coverage limits, filter the shortlist, and
   select a candidate or a deliberately difficult comparison case.
2. **Verify:** select an account pair with the graph or keyboard-accessible selector,
   inspect its repeated episodes and source records, replay arrivals, and consider
   the surrounding activity and strongest-family removal result.
3. **Review and brief:** inspect community context beside the review form, save a
   reasoned decision, preview its saved revision, and download that revision's brief.
   Unsaved drafts remain visibly separate from the saved export and survive case
   switching within the browser session.

- A synthetic dataset of 89 posts and 1,137 comments across three fictional channels.
- Repeated first-arrival detection with constrained randomized controls and inspectable
  pair evidence. Comment wording and narrative labels do not affect ranking.
- A timing matrix, episode replay with surrounding comments, and clickable Cytoscape
  connections with their observed and randomized recurrence counts.
- Organic-outage, prolific-critic, and openly coordinated volunteer comparison cases.
  The volunteers can be detected as coordinated without establishing hostile intent.
- A community timeline, separate contextual narrative annotations, and persistent
  reviewer decisions with revision history, unsaved-change state, and pinned saved
  brief previews/exports.
- A strongest-episode removal check that reruns the detector, plus Markdown, JSON,
  and CSV exports containing the selected evidence and provenance.
- An explicit normalized JSONL importer and completed-ZIP inventory command. The
  challenge archive still needs an inspected, source-specific mapping.

## Run locally

Install Python 3.12 and [uv 0.11.3](https://docs.astral.sh/uv/getting-started/installation/).
From `swarm-detector/`, run `make -C .. setup` when installing or updating the
shared development environment. It installs all three packages into the root
`.venv` using the root `uv.lock`. A member-only `uv sync` can prune dependencies
from that shared environment. See the [development guide](../docs/PYTHON_DEVELOPMENT.md).

After setup, stay in `swarm-detector/` so the default database resolves to
`swarm-detector/data/swarm.db`. Keep the same `SWARM_DATA_DIR` or `SWARM_DB_PATH`
if you already use an explicit database location.

```sh
uv run --no-sync swarm-detector demo
uv run --no-sync swarm-detector serve
```

Open [http://127.0.0.1:8050](http://127.0.0.1:8050). The development server binds to
the local machine by default. `demo` is an idempotent synthetic seed-and-analysis
command. Application startup seeds synthetic examples only when the database is
empty, including when served through Gunicorn or Docker. Imported datasets are not
overwritten. No startup command downloads or uploads the challenge dataset.

The examples are deliberately synthetic. They demonstrate recurring coordination,
organic activity, and difficult counterexamples; they do not establish accuracy on
the real challenge dataset. A fresh container opens with the same labelled synthetic
demo, while a container with an existing imported database retains its data.

To use environment settings without exporting them into your shell:

```sh
cp .env.example .env
uv run --no-sync --env-file .env swarm-detector serve
```

| Setting | Default | Purpose |
| --- | --- | --- |
| `SWARM_DATA_DIR` | `./data` | Local working data directory. |
| `SWARM_DB_PATH` | `<SWARM_DATA_DIR>/swarm.db` | Optional explicit database location. |
| `SWARM_BASIC_AUTH_USER` / `SWARM_BASIC_AUTH_PASSWORD` | Both unset | Optional HTTP Basic authentication; configure both or neither. |
| `PORT` | `8050` for Gunicorn | Container/web-service listening port. The development CLI also accepts `--port`. |

Basic authentication is a small-team access barrier, not a user/role management system.
Use HTTPS when the service is exposed beyond your machine. `/health` remains
unauthenticated and returns readiness without source comments or case details.

## Inspect and import data

Keep the original archive in `dataset/`, outside version control and container builds.
The download is approximately 48.3 GB; the archive's listing should be inspected before
choosing what to extract. Do not rename or process a browser's `.crdownload` file as a
completed ZIP.

```sh
uv run --locked swarm-detector inspect-archive 'dataset/Swarm Detector_Dataset_Challenge.zip'
uv run --locked swarm-detector import-jsonl /absolute/path/to/normalized.jsonl
uv run --locked swarm-detector analyze
```

`inspect-archive` lists archive metadata without extracting it. The current importer
accepts the normalized JSONL contract below. It does **not** infer an undocumented
Telegram export schema or import arbitrary attachment/analysis folders directly.
Archive-specific mapping requires inspection of the completed dataset first.

Each UTF-8 JSONL line is an object with `record_type` set to `metadata`, `channel`,
`post`, or `message`. Supply metadata first, followed by channels, posts, and messages.
For example:

```jsonl
{"record_type":"metadata","dataset_id":"example-import","label":"Example import","synthetic":false,"manifest":{"identity_stable_across_channels":false,"timestamp_precision_seconds":1}}
{"record_type":"channel","channel_key":"channel-001","name":"Example channel","category":"unknown","rationale":"Classification has not been reviewed."}
{"record_type":"post","post_key":"post-001","channel_key":"channel-001","post_family_key":"family-001","timestamp_utc":"2026-10-14T12:00:00Z","title":"Example post","text_original":"Example post text.","source_ref":"input:post-001"}
{"record_type":"message","message_key":"message-001","post_key":"post-001","account_key":"account-001","channel_key":"channel-001","timestamp_utc":"2026-10-14T12:01:00Z","text_original":"Example comment.","urls":[],"source_ref":"input:message-001","sender_type":"user"}
```

Use source identifiers, not display names, to identify accounts. Set
`identity_stable_across_channels` to true only when the source supports that claim;
otherwise identities are scoped to their channel. Preserve original-language text,
source references, and timestamp precision. Missing account identities cannot support
account-level coordination and are excluded/reported. Timestamps must be timezone-aware
UTC values, and references must resolve to imported channels/posts. Use
`post_family_key` to group reposts or duplicate discussions of one underlying episode
so they do not inflate independent evidence. An unknown channel category is acceptable.

After importing and analyzing a different dataset, reload the browser so its dataset
header and saved analysis agree. Keep source inspection and normalization local until
you explicitly choose a hosting destination.

## Review and export

The workbench links candidate groups to concrete episodes and comments. Review the
timing matrix, graph connections, surrounding context, and counterevidence before
recording a decision. Narrative annotations are a separate review step and do not
raise the behavioural coordination score.

Replace `CASE_ID` with a case identifier shown by the workbench:

```sh
uv run --locked swarm-detector export CASE_ID --format markdown --output outputs/case.md
uv run --locked swarm-detector export CASE_ID --format json --output outputs/case.json
uv run --locked swarm-detector export CASE_ID --format csv --output outputs/case.csv
```

Exports can contain source comments and account identifiers. They remain local unless
you deliberately share them; `outputs/` is excluded from Git and Docker builds.
The CLI commands above use the active saved run and latest review. For the exact saved
revision shown in the interface, use its download action. Programmatic callers can
pass `run_id` and `revision` to `Workbench.brief_preview()` and `export_case()`;
revision zero deliberately pins an unreviewed case. Complete exports contain the
selected case's records, while dashboard responses use bounded pages with totals.
Thread share counts core-account comments inside the selected case threads against
all observed comments in those same threads. The community timeline can show those
accounts' broader activity elsewhere in the imported dataset. Participation shares do
not measure persuasion.

## Package structure

Application startup, presentation, analysis, and persistence have separate entry points:

| Module | Responsibility |
| --- | --- |
| `config.py` | Frozen, validated timing and randomization settings. Rejects unknown settings and invalid numeric values. |
| `ingest.py` | Normalized JSONL validation, deduplication, source references, and archive inventory. |
| `detector.py` | First arrivals, recurring pairs, randomized controls, and candidate groups. No UI or database access. |
| `models.py` | Validated source, case, evidence, run, review, and page records. |
| `storage.py`, `persistence/` | Compatibility facade and focused repositories, transactional schema upgrades, normalized read tables, and backup. |
| `service.py` | Shared application operations, pinned run/review identity, and bounded case reads. |
| `readiness.py` | Shared readiness facts and report formatting for SQL and existing wire-record callers. |
| `export.py` | Case-scoped Markdown, JSON, and CSV serialization. |
| `ui.py` | Display formatting, charts, graph elements, and tables built from supplied records. |
| `presentation.py` | Case, evidence, queue-card, review-form, and removal-result views, rendered from supplied records. |
| `layout.py` | Declarative dashboard component layout. |
| `web.py` | The `create_app(workbench=None)` factory, authentication, health endpoint, callback bindings, and interaction state. |
| `wsgi.py` | Explicit application startup for Gunicorn: `swarm_detector.wsgi:server`. |
| `cli.py` | Command parsing and invocation of application operations. |
| `sample_data.py` | Fictional fixtures, used only by explicit startup/bootstrap operations. |

Importing the web factory does not initialize a database, seed data, or run analysis.
Call `create_app()` to start an application, or supply a `Workbench` with an explicit
database path. Interactive overview, readiness, queue and case reads use normalized
SQL rows and aggregates. Case pages default to 200 rows per collection, with an
explicit maximum of 500 and totals/truncation metadata. Public responses are fresh
records and keep the same pinned run and review revision across later updates.

Full analysis and explicit snapshots still materialize data. Complete-case exports
accumulate selected evidence pages; the normalized importer and legacy migrations
also have memory costs. Read [the persistence and paging contracts](docs/PERSISTENCE.md)
for guarantees, migration behavior, revision pinning, and measured limits.

Layout, presentation, and callback bindings are separate from analysis.
Presentation functions receive explicit inputs and can be tested without a database;
callbacks own service calls, selection state, and browser output bindings.

## Verify changes

```sh
make -C .. test-swarm
make -C .. lint
make -C .. locks
docker compose config --quiet
```

Tests also cover import-time side effects, explicit WSGI startup, mutable response
isolation, consistent run identity during case assembly, invalid detector settings,
malformed normalized records, populated chart rendering, and the explicit source-text
reveal safeguard. Ruff checks modern Python idioms, imports, and
common bug patterns, with one formatter for the package. Backend tests additionally
cover original unversioned upgrades, rollback, future-version rejection, concurrent
migration openers, typed validation, SQL-only page reads, bounded allocation growth,
and run/revision-pinned review and export behavior.

These commands run the current automated checks; they do not validate the detector on
the real dataset. Inspect individual findings and record uncertainty. Shared opinions,
high activity, a single news burst, and cross-community participation are insufficient
on their own to establish a swarm. Coordination can also be legitimate human activity.

## Evaluate the synthetic workflow

```sh
uv run --locked python scripts/evaluate_sample.py
```

The standalone evaluator uses generated fixtures and a fresh temporary SQLite database.
It does not open the working database or the challenge archive. It writes
`evaluation/review/sample-results.json` and `evaluation/review/reviewer-packet.md`.
Use `--output-dir /absolute/path` for another report destination, or `--controls N` to
change the recorded number of fixed-seed randomized controls.

The checks cover candidate recovery on designed fixtures, organic outrage and a prolific
critic, transparent public coordination, exact source references, wording/label
invariance, deterministic reruns, saved review revisions, three export formats, and
SQLite backup. The public volunteers can be detected as coordinated while automation
and hostile intent remain unestablished. A technical save/reopen probe is explicitly
labelled as automated and does not count as a human review.

The review checks also reject a stale write, preserve revision zero and earlier saved
previews, keep exports pinned when the active run changes, and compare complete
JSON/CSV exports against case totals rather than a bounded UI page.

The results record actual check counts, settings, input fingerprints, source hashes,
machine/software scope, and timing/memory measurement limits. They are pipeline sanity
checks, not real-world accuracy estimates. The packet leaves human decisions and
narrative validation incomplete.

## Docker

From `swarm-detector/`, use Compose to run the local service on port 8050. The
Compose file builds from the EDTH root and uses the shared lockfile.

```sh
docker compose up --build -d
docker compose exec app swarm-detector demo
docker compose logs --tail 100 app
```

Open [http://127.0.0.1:8050](http://127.0.0.1:8050). Compose binds only to the local
interface. The image runs as a non-root user and uses one Gunicorn application worker
with four threads. Its named `swarm-data` volume persists `/var/data` across container
replacement. `docker compose down` retains that volume; adding `--volumes` deletes it.

The root `.dockerignore` allowlist excludes original datasets, local databases,
exports, `.env`, and host virtual environments. The final image contains the
installed Swarm package and its assets, built with production dependencies from
the shared lock.

There is no host dataset mount by default. To import a specific local normalized file
without mounting Downloads or the entire project:

```sh
docker compose run --rm --no-deps \
  --volume /absolute/path/to/normalized.jsonl:/imports/normalized.jsonl:ro \
  app swarm-detector import-jsonl /imports/normalized.jsonl
docker compose exec app swarm-detector analyze
```

This is an explicit local import into the persistent volume. Allow a long import or
analysis to finish before starting another write job.

To build and verify with disposable synthetic storage, run from the EDTH root.
The smoke runner uses the Python standard library and Docker.

```sh
smoke_dir=$(mktemp -d "${TMPDIR:-/tmp}/swarm-root-lock-smoke.XXXXXX")
smoke_image="swarm-detector:root-lock-smoke-${smoke_dir##*.}"
docker build --file swarm-detector/Dockerfile --tag "$smoke_image" . &&
  python3 swarm-detector/scripts/smoke_container.py \
    --image "$smoke_image" --output "$smoke_dir/container-results.json"
```

The smoke runner publishes no host port and uses an isolated container and a new
volume. It checks readiness, authentication, packaged CSS, and a labelled technical
review, then verifies restart persistence and a SQLite backup. It removes its test
container and volume and writes the result to `$smoke_dir/container-results.json`.
Existing services, volumes, and virtual environments stay in place.

## Back up the database

```sh
uv run --locked swarm-detector backup backups/swarm-backup.db
docker compose exec app swarm-detector backup /var/data/swarm-backup.db
```

The command uses the [SQLite backup API](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.backup).
Do not copy a live database file by itself: committed data can still be in its WAL.
Copy a completed backup to a separate approved location; a backup on the same volume
does not protect against losing that volume. Restore with the application stopped,
keeping the previous database and any WAL/SHM files together until recovery is verified.

## Optional Render deployment

`swarm-detector/render.yaml` is a deployment template for the standalone Swarm
service. The local smoke test covers the Docker image.

It defines one Docker web service, a `/var/data` persistent disk, and Basic-auth
credentials supplied during setup. Its `starter` plan is a supported legacy name;
confirm the current plan and cost before creating it. The 1 GB disk is for the small
demo database, not the 48.3 GB original archive. Increase capacity based on measured
database and backup sizes. Auto-deploys are disabled in the template.

Connect the EDTH repository and enter `swarm-detector/render.yaml` in Render's
**Blueprint Path** field. Leave the service root at the repository root. The
template sets `dockerContext: .` and `dockerfilePath: ./swarm-detector/Dockerfile`
so the build can read the root lock. Preview the configuration before creation.
See [Render Blueprint setup](https://render.com/docs/infrastructure-as-code).

Set both authentication values and verify `/health` and the labelled synthetic
demo on a fresh disk. An existing imported database is retained across deploys.

The image creates a shell-enabled non-root user and its private `.ssh` directory so
Render's platform shell can support these explicit maintenance commands; it does not
run an SSH server. See [Render's Docker shell requirements](https://render.com/docs/ssh).
Verify that the mounted disk is writable by UID 10001 during deployment setup.

Render disks are available only to their attached service at runtime. Do not add an
independent worker, pre-deploy job, or one-off job expecting to share this database.
Keep one service instance, and run deliberate CLI jobs in that service's shell. Disk
deploys have a brief service interruption. These limits follow Render's
[disk documentation](https://render.com/docs/disks) and
[Blueprint reference](https://render.com/docs/blueprint-spec).
