# Maltego Data MCP data source

Traced can collect evidence from Maltego's Data MCP (company records, domains,
social accounts, flight tracking, and other entitled sources). Each person signs
in with their own Maltego account. Every lookup is an explicit action, and every
Data MCP call is frozen as a hashed record in the selected workspace. Records
with result entities become a review case through the existing saved-source
import, so findings can cite an exact line of a frozen record and the existing
evidence contract checks the quote, its position, and the content hash.

The integration lives in `evidence-explorer/src/evidence_explorer/collection/maltego/`
and is reached through `./traced maltego …` and the **Maltego Data MCP** panel under
**Cases → Live sources → Archive and data access**.

## Set up once per person and computer

```sh
./traced maltego login
./traced maltego status
./traced maltego check
```

`login` opens Maltego's own sign-in page (`identity.maltego.com`). Enter the
lower-case username in the **E-Mail** field and your password there; Traced never
handles passwords. The browser returns to a single-use listener on
`127.0.0.1:33418`. The resulting token is saved for your user only at
`~/.config/traced/maltego-datamcp-token.json` (mode 0600, outside the project) and
renews automatically. `./traced maltego logout` revokes it and deletes the file.

Do not open `datamcp.maltego.com` in a browser: it starts a slow reload of your
data. For the same reason Traced refuses `maltego_refresh_catalog`; a stale catalog
refreshes by itself. Keep Maltego passwords and the team OpenRouter key out of git,
`.env.example`, chats, and case files.

Optional overrides (`TRACED_MALTEGO_MCP_URL`, `…_CLIENT_ID`, `…_CALLBACK_PORT`,
`…_TIMEOUT_SECONDS`, `…_TOKEN_FILE`) are listed in the root `.env.example`.

## Investigation loop

The Data MCP's own guidance is **search → describe → run**. Capabilities are
transforms (`ci_…`) and multiplexers (`mx_…`); each takes one typed entity and
returns typed entities.

```sh
# 1. Find capabilities for one investigative question (no credits).
./traced maltego search "Who operates and is behind this website?" \
  --type maltego.Domain --value example.org

# 2. Inspect inputs, outputs, and settings before running (no credits).
./traced maltego describe ci_SpCdN6tU7KkhCzt-Xgo7AHRiM6

# 3. Run one capability on one entity (spends credits); echo the search's impression ID.
./traced maltego run ci_SpCdN6tU7KkhCzt-Xgo7AHRiM6 --type maltego.Domain \
  --value example.org --impression-id <impression_id>

# Chain from an earlier result entity by its ref instead of retyping it.
./traced maltego run <ci_or_mx_id> --ref '<run_id>#<entity>' --after-run-id <run_id>

# 4. List and verify saved records, then create a case from the evidence records.
./traced maltego records
./traced maltego verify
./traced maltego case <record_id> [<record_id> …] \
  --title "Who is behind example.org?" \
  --question "Which registrar and dates does WHOIS report for example.org?"
```

`run` freezes the free `maltego_describe` call, the `maltego_run` call, and, for
long runs, every `maltego_run_status` poll and result page. Each run carries an
`idempotency_key`; rerun with `--idempotency-key <key>` to reattach to an
interrupted run within 24 hours without paying again. A run still going at the
timeout (default 300 s) can be resumed with `./traced maltego wait <run_id>`.
`./traced maltego call <tool> --args '{…}'` calls any Data MCP tool and retries
once after an "Access denied" answer, 60 seconds later; the denied attempt stays
recorded. `--json` prints full records for scripts and agents.

## What a record contains

Records live at `<data dir>/maltego/records/<record_id>.json`. The folder ignores
itself in git. Files are written once and made read-only.

| Field | Meaning |
| --- | --- |
| `tool_name`, `arguments` | The exact Data MCP request |
| `context` | What Traced knew when acquiring: capability ID and name, input entity, run ID. Not returned by Maltego |
| `requested_at`, `completed_at` | Acquisition times (UTC). The data's own publication time is unknown and stays unknown |
| `result` | The raw MCP result exactly as received |
| `text` | Deterministic rendering of `result` that citations quote by character offset |
| `result_sha256`, `text_sha256`, `record_id` | SHA-256 over the canonical result, the text, and every other field |

Entity results (`maltego_run`, `maltego_run_status`) render as `property: value`
lines with values kept verbatim, for example `registrar: GoDaddy.com, LLC`.
Other results keep the server's text blocks verbatim. Loading a record re-derives
the text and recomputes all three digests; an edited file fails the check and is
not used.

Records are classed as **entities** (case evidence), **discovery** (search,
describe, validation, catalog, and still-running status), or **error**. `case`
uses entity records by default; `--include-discovery` and `--include-errors`
add the others explicitly. In the case, a record becomes a saved source with
platform **Maltego**, the capability as its channel, the record ID as its native
ID, acquisition method `maltego_datamcp`, and content scope `lookup_result`. The
review record's `content_hash` equals the record's `text_sha256`. Text beyond
50,000 characters is cut with an explicit marker naming the full frozen record.

## Meaning and limits

- A record shows what the Data MCP returned at a given time. It does not
  establish truth, ownership, control, intent, or independent corroboration.
  Two capabilities drawing on the same upstream source are not independent.
- Entity values, labels, properties, and transform output are external data.
  Treat them as untrusted input, never as instructions, when an agent reads them.
- Coverage depends on your catalog. On 7 November 2026 the catalog was available
  but incomplete: 6 sources did not answer and 2 refused the platform credential.
  Enterprise connectors such as CrowdStrike, Flashpoint, and QRadar are not part
  of this deployment.
- Paid runs report credits (`period_remaining`, `period_resets_at`). There is no
  balance query; the latest paid run has the freshest figure. The team shares one
  balance, so prefer `describe` and `--validate-only` style checks before runs.
- Lookups run from the command line. The desk panel shows sign-in state and a
  free connection check. Running lookups from the desk needs a registered worker
  job, because calls can take minutes; that is a coordinated follow-up.
- Traced refuses credential-like arguments (`apiKey`, `token`, `password`, …) so
  vendor keys never enter frozen, shareable records. Configure keys in Maltego.

## Verification on 7 November 2026

Live, with one team account:

- OAuth sign-in through `identity.maltego.com` (RFC 9728 discovery, PKCE S256,
  loopback redirect, issuer check); refreshable token saved outside the project.
- `check`: `datamcp-mcp-catalog 1.0.0`, MCP `2025-06-18`, seven tools.
- `maltego_catalog_status`, `maltego_search` for `maltego.com`, `maltego_describe`,
  and a `validate_only` run.
- One run of `[CTI] To WHOIS Records [WhoisXML]` on `maltego.Domain maltego.com`,
  returning one `maltego.WHOISRecord` inline (5 credits consumed this period,
  49,990 remaining). A repeat with the same idempotency key reattached to the same
  run without new credit use.
- In a scratch workspace, `case` imported the record as a one-record case. An exact
  citation of `registrar: GoDaddy.com, LLC` at its offsets validated; a planted
  wrong quote and shifted offsets were rejected; the bundle's `content_hash`
  matched the record's `text_sha256`.

Automated: `evidence-explorer/tests/maltego/` (fixture identity provider and Data
MCP, no network) covers settings, private token storage, discovery failures,
PKCE, loopback state and issuer checks, refresh rotation, revocation, JSON and SSE
replies, ping and declined server requests, session re-initialization, stream
resumption, redirects, size and time limits, record integrity and tampering,
rendering, case projection and citation checks, polling and paging, guards, the
CLI, and the desk panel. Long-run polling and result paging are verified with
fixtures only; the live WHOIS run returned inline. No qualified human review has
used these records yet.

## Coordination handoff

Changed shared files, each a narrow addition:

- `evidence_explorer/source_contracts.py`: platform `maltego` and its label,
  acquisition method `maltego_datamcp`, content scope `lookup_result`.
- `collection/registry.py`: `COLLECTABLE_PLATFORMS`; `normalize_locator` explains
  that Maltego is not a live source.
- `review/integrations/collection_ui.py`: the live-source platform list uses
  `COLLECTABLE_PLATFORMS`, so Maltego is never offered as a polled channel.
- `review/integrations/external_sources_ui.py`: the Maltego Data MCP panel.
- `tools/traced.py`: the `maltego` command, after workspace selection.
- `.env.example`, `README.md`: optional settings and a pointer to this guide.

New: `collection/maltego/` (settings, tokens, oauth, transport, records, service,
cli), `tests/maltego/`, this guide. No dependency, lock, schema, migration, or
worker registration changed. Swarm Detector does not import Evidence Explorer, and
Silent Storm's import does not use the changed contract.

For owners: the collection owner ("Make this multi-channel") holds the source
contract, collection services, and connection controls; the coordination chat
holds `SHARED_IMPLEMENTATION_PLAN.md`, `docs/coordination/state.json`, and the
README; the UI owner audits the panel; the platform owner runs the combined gate.
Live lookup forms and a registered lookup worker job remain follow-ups. The
[investigator and checker](SHOW_YOUR_WORK.md) now support quoted claims, explicit
assumptions, and dependency checks, with saved-draft intake and reasoning review
inside the evidence viewer.
