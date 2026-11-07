# Show your work: investigator and checker

For the EDTH "Show your work" challenge, Traced pairs a basic **investigator**
with a **checker** over the Maltego Data MCP data source. The investigator runs
a few explicit lookups and has a model write claims. Each claim has reasoning
steps, exact quotes from frozen records, and explicit assumptions. The checker
follows every link back to its saved record, labels it, and shows where each
conclusion's chain is weak. The checker is the priority. It is cheap, mostly
deterministic, and works on any bundle that uses the shared evidence contract,
whatever the entity type.

The reusable engine is packaged in `evidence_explorer.investigation`, so the
installed evidence viewer and the `./traced-investigate` command use the same
checks. Existing `tools.investigation` imports remain compatible. Tests live in
`evidence-explorer/tests/investigation/` and `tools/tests/test_investigation.py`.

## In the evidence viewer

Keep the existing **Plan investigation → Gather evidence → Confirm scope →
Investigate findings → Review conclusions → Prepare and share briefing** flow.
The investigation question and confirmed source scope stay in the saved plan.

1. On **Cases → Gather evidence → Open saved investigator draft**, refresh the
   drafts in the selected workspace, preview one, and choose **Open draft for
   review**. The viewer imports that exact saved bundle and selects its claim's
   analysis run. It keeps collection observations and earlier reviews intact.
   JSON bundles from other agents still use the ordinary case import.
2. To write a claim from already saved sources, open **Write a checkable claim**
   on Cases. Start a draft for the selected case, choose a saved text record,
   and supply a conclusion, reasoning step, exact quotation and any assumption.
   Saving creates a new, explicitly manual draft run. It does not approve the
   claim, confirm scope, change the source, or call a model. Switching cases
   keeps the form but disables saving against the wrong evidence.
3. On **Review → Check the reasoning**, choose **Check saved reasoning**.
   Mechanical and rule-based checks run on the complete saved case, with
   conclusions shown for the selected analysis run. The report puts unsupported
   or conflicting dependencies first, shows the reasoning path, and opens the
   exact saved source through the existing inspector. Long quotations retain
   their full text. Missing original Maltego files remain visibly unverified.
4. Record **Use**, **Qualify**, or **Withhold** using the existing decision
   controls, then prepare the cited briefing and portable case as usual. Drafts
   and decisions retain their exact run and finding identities. Download the
   reasoning report separately when its detailed checks are useful to a reviewer.

The interactive checker makes no network or model calls. Its report is held in
memory and can be downloaded; changing the evidence or assessment requires a
fresh check. Excluded evidence, unsupported media/text selectors, and cases above
the interactive size/work limits make this check unavailable. Existing evidence
inspection, specialist methods and review decisions remain accessible. Optional
model assistance is still explicit through **Request an evidence check** or the
CLI; a machine result never becomes a human decision.

This supports the cited-claim and election-briefing use cases: move from a bounded
saved sample to a substantive, checkable claim before a reviewer decides what to
brief. Keep separately scoped events in separate cases. Neither a matching quote
nor a clean reasoning report establishes historical completeness, factual truth,
independent corroboration, intent, or a monitoring alert.

For the NYC 2025 briefing exercise, keep the June 24 primary and November 4
general election in separate cases, each with its own two-calendar-month window
before and after the event and the `America/New_York` timezone. Confirm those
bounds and the selected public sources in the existing plan. Draft claims from
the actual saved sample, cite exact passages, expose assumptions, and let the
reviewer decide what belongs in the briefing. A saved sample does not imply a
complete election archive; live alerts and submissions remain separate actions.

## Status on 7 November 2026

| | State |
| --- | --- |
| Implemented | Packaged investigator/checker shared by the CLI and viewer; saved-draft intake, manual cited claims, explicit saved-reasoning checks, dependency traces, and downloadable reports. Existing human decisions and portable case export are reused |
| Original CLI verification | 38 offline tests (fixture records built by the real `build_record`, fake verifier, fake Data MCP source, mocked OpenRouter) and the then-current 84 tool tests passed. A checked bundle imported into a scratch review workspace |
| Viewer verification | Focused integration, import/binding, stale-state, note-preservation and UI checks; shared browser gate; all six distributions built and fresh-install checks passed. A fictional browser exercise verified saved-draft import, exact evidence navigation, manual drafting, planted contradiction, report download and case export/reopen at desktop and 390 px in light/dark themes. No source or model calls were made |
| Verified live | Maltego on `maltego.com`: free search, describe, and three validate-only checks; two paid runs (historical WHOIS, IBM Watson WHOIS entities), 6 credits. Mechanical and deterministic checks of a hand-written draft over the three frozen records, and the planted assumption caught. See [Live run](#live-run-who-is-behind-maltegocom) |
| Not run | The investigator's model draft and the model-based reasoning checks: OpenRouter is not configured (`OPENROUTER_API_KEY` and `EVIDENCE_REVIEW_MODEL` not set). Reports say so explicitly; [re-run commands](#re-run-with-the-model-once-openrouter-is-configured) are below |
| Human review | None. Labels are machine checks for a reviewer, not review decisions |

## Quick start

```sh
# 1. Create an investigation; search and describe capabilities (free).
./traced-investigate start "Who is behind example.org?" --type maltego.Domain --value example.org

# 2. Check entitlement without executing (free), then run what you chose (credits).
./traced-investigate lookup <folder> --validate-only --capability <ci_id>
./traced-investigate lookup <folder> --capability <ci_id> [--capability <ci_id> …]
#    …or reuse records already frozen in this workspace (free):
./traced-investigate lookup <folder> --record <record_id>

# 3. Have the model draft claims from the evidence records → bundle.json.
./traced-investigate write <folder>             # needs OPENROUTER_API_KEY + EVIDENCE_REVIEW_MODEL
./traced-investigate write <folder> --draft my-draft.json --draft-note "who wrote it"  # or by hand
#    An existing bundle.json is kept unless you pass --replace.

# 4. Check: report.json, report.md, checked-bundle.json under <folder>/check/.
./traced-investigate check <folder>             # --reasoning off for deterministic only

# 5. Demo: copy the investigation with one planted assumption, then check the copy.
./traced-investigate plant <folder> --statement "…" --into C2
./traced-investigate check <folder>-planted
./traced-investigate show <folder>-planted --markdown
```

`<folder>` is a path or a folder name under `<data dir>/investigations/`. The
workspace is resolved exactly as `./traced` resolves it (`--data-dir`,
`EVIDENCE_DATA_DIR`, root `.env`). `run "<goal>" --type … --value … --capability …`
does steps 1 to 4 in one go. It spends credits only on the capabilities named
(or on `--auto N`).

## What the investigator writes

An investigation folder (`<data dir>/investigations/<stamp>-<slug>/`, ignored by
git like the record store) holds:

- `investigation.json`: goal, subject, plan (search and describe record IDs,
  impression ID, candidates), every lookup (capability, status, run ID,
  idempotency key, record IDs, and the credit figures the run reported), the
  evidence record IDs, the drafting model and prompt digest, and the checks run.
- `draft.json`: the model's structured answer. Each step cites `{record, line,
  quote}`. The model never supplies offsets.
- `bundle.json`: an ordinary `EvidenceBundle`. Records come from the existing
  saved-source projection (`build_source_import` → `build_source_bundle`), so they
  carry the same IDs, `content_hash` (= the record's `text_sha256`), provenance,
  and 50,000-character truncation marker as `./traced maltego case`. Claims and
  steps are `inference` findings and assumptions are `assumption` findings.
  Arrows run premise → finding: a cited record `supports` a step (with an exact
  fragment), a step `supports` its claim, and an assumption is a `depends_on`
  premise of the step or claim that uses it.
- `citations.json`: how each quote was located (exact on the cited line, found
  on another line, or rejected). A rejected quote stays on its step as
  `rejected_citations`; it is never silently repaired.

The drafting prompt treats records as untrusted data. It forbids background
knowledge, requires verbatim quotes, and asks for every assumption beyond the
quoted lines (for example "the registrar is the operator", "a privacy service
hides the registrant", "the data is current") to be stated separately.

## What the checker checks

The checker reads bundle JSON as plain data, so an edited or broken bundle still
gets a report.

1. **Mechanical (exact).** Each record's text hashes to its `content_hash`. A
   Maltego-derived record is re-loaded from its frozen file, which re-derives
   the text and recomputes all three digests, and must equal the saved-source
   text. Each fragment's quote must sit at exactly its offsets; otherwise the
   report says where the quote really is, or that it occurs nowhere. Every link
   must resolve, and every reasoning step must cite at least one record.
2. **Deterministic reasoning (heuristic, no model).** It works on typed values
   (dates in common formats, domains, emails, IPv4, URLs, numbers, quoted strings,
   names) and on property concepts from `property: value` keys (creation, expiry,
   registrar, registrant, operator, DNS, hosting, location, contact, …):
   - *grounding*: values a step asserts must occur in the quotes it cites;
   - *out of role*: a value shown only under an unrelated property, such as a
     `registrar` line quoted as proof of an operator, does not support the step;
   - *conflict*: a `property: value` line naming the same property with a
     disjoint value contradicts the statement. Only values natural to the
     property are compared: dates under creation, update, or expiry; domains
     under DNS, registrar, or hosting; emails under contact; IPs under IP, DNS,
     or hosting. So a street line that mentions a domain is ignored. A cited line
     that disagrees is a misread and always counts. Any other line counts only
     when no saved line agrees. Records often hold several snapshots, such as
     historical WHOIS, and an older value is listed as a *caveat* ("another
     record or snapshot gives …"), not a contradiction;
   - *assumption backing*: an assumption is backed only when a record line names
     its values under a matching property;
   - *unstated reinterpretation*: a conclusion that puts a value in a role its
     evidence does not show, without a stated assumption, is flagged at the
     inference link.

   Values that the question itself gives (the subject) are not checked.
   Free-form names are only checked for presence, never declared conflicting.
3. **Targeted model checks (optional).** The existing review verifier
   (`OpenRouterVerifier`, prompt `evidence-challenge-v3`, temperature 0, strict
   JSON) challenges a step against its cited lines plus a few related lines from
   other records. It challenges an assumption against the most related record
   lines, and a conclusion against its evidence plus its declared assumptions.
   An objection counts only if every quote is verbatim in a passage it was shown;
   other objections are discarded and listed. Answers are cached by an input
   digest in `check/reasoning-cache.json`, so a re-check is free and identical.
   A model check can add a problem or settle an open question. It never clears a
   deterministic failure. When model checks do not run, the report header and
   every open link say so and why, for example "not run: OpenRouter is not
   configured". A link left `unchecked` is never presented as passed.

### Labels

Every premise link gets one label; the worst applies.

| Label | Meaning |
| --- | --- |
| `supported` | Passed every check that applies. Says nothing about truth |
| `unchecked` | No check could decide (no checkable value or property; model off or failed) |
| `assumption_not_backed` | No saved record line backs this assumption |
| `unsupported` | Broken or missing citation, value not in the quote, out-of-role quote, rejected (fabricated) quote, unstated reinterpretation, or a verified model objection |
| `contradicted` | A record line gives a disjoint value for the same property (deterministic), or a verified model contradiction |

A conclusion **holds** when every link in its chain is supported. It is **weak**
when its chain contains unbacked assumptions or unchecked links, and
**undermined** when the chain contains an unsupported or contradicted link. The
report lists each flagged link with its path (for example `C2 → A4`), the exact
record line, and the reason. `checked-bundle.json` is the original bundle plus a
checker run, `contradicts` links from the conflicting record lines, and
`supports` links for assumption backing. It validates as an `EvidenceBundle`
and can be imported into a scratch workspace for review
(`./traced --data-dir <scratch> review import <file>`).

## Demo: a planted bad assumption

`plant` copies an investigation and adds one assumption that a chosen claim or
step relies on. The planted finding is labelled fictional everywhere a person
looks: its own run (`planted_for_checker_demo`), `details.planted`, and a case
limitation. The answer key goes to a separate `planted.json`. The checker reads
none of these markers, and a test shows the labels are identical when the
markers are stripped.

Offline fixture (`PlantedAssumptionDemoTests`): the honest `example.org` draft
has C2 *holds* and C1 *weak* (it rests on "the WHOIS data is current"). Planting
"example.org was first registered in 2018, when its current owner bought it"
into C2 turns C2 *undermined*:
`contradicted (via C2 → A2): R1 line 8 creationDate: the record gives 2009-03-14
where A2 says 2018`. C1 is unchanged, and A2 is the only contradicted finding.

## Live run: who is behind maltego.com?

Workspace `evidence-explorer/data/evidence-trail`, folder
`investigations/20261107T131420Z-maltego-com-who-is-behind-maltego-com`.

| Step | Calls | Credits |
| --- | --- | --- |
| `start` | `maltego_search` (8 results), `maltego_describe` (6 candidates) | 0 |
| `lookup --validate-only` | historical WHOIS, IBM Watson WHOIS entities, Aleph company registries: all `valid` (no cost estimate returned) | 0 |
| `lookup --record` | reused the earlier current-WHOIS record `a3b685cdfeea…` | 0 |
| `lookup --capability` | `[CTI] To Historical WHOIS Records [WhoisXML]`: 24 snapshots, record `f3f0fd0e8c74…` | 5 |
| `lookup --capability` | `[Utilities] To Entities From WHOIS [IBM Watson]`: 14 entities, record `67edbc9e08cf…` | 1 |

Paid runs reported `period_consumed_total` 10 then 11 (it was 5 after the
earlier WHOIS run) and 49,984 remaining. The per-run figures assume no
teammate ran a lookup in between. There is no balance query.

What the records show: registrar `GoDaddy.com, LLC`, creation date 2007-10-09,
and in every historical snapshot (2024-10 to 2026-10) the registrant
organisation `Domains By Proxy, LLC` with placeholder contact names. The current
RDAP record names no registrant. The IBM Watson record is noisy NER output (for
example `maltego.Person · GoDaddy.com, LLC`). The historical record (119,837
characters) enters the case as its explicit 50,000-character saved-source
prefix.

### Draft, check, and planted assumption (deterministic checks only)

The investigator's model could not draft, because OpenRouter is not configured.
The draft was therefore written by hand by the Claude side session from the
three frozen records (`hand-written-draft.json`). The bundle records this as
`draft_source: file:hand-written-draft.json` with a `draft_note`. The draft has
3 claims, 2 assumptions, and 7 citations, with 0 rejected. Model checks were
**not run**, for the same reason, and each report says so.

`check` (bundle `b0514c5e6a0ceddc…`): all three records re-verified against
their frozen files. Links: 14 supported, 2 assumption not backed, 0 unchecked,
0 unsupported, 0 contradicted.

- **C1 weak**: "maltego.com was created on 2007-10-09 and is registered through
  GoDaddy.com, LLC". It rests on A1 ("the WHOIS lookups describe the
  registration as it stood when they ran"), which no record states.
- **C2 weak**: "The WHOIS data does not name the party behind maltego.com …". It
  rests on A2 ("Domains By Proxy, LLC is a privacy service … rather than
  operating the website"). The checker reports that the proxy's name appears
  only as `registrantOrganization` and that no record line says anything about
  operating. A caveat on C2.S3 notes that older snapshots give other
  `updatedDate` values.
- **C3 holds**: the IBM Watson record types the registrar `GoDaddy.com, LLC` as
  a person.

`plant --into C1` added A3, "maltego.com was first registered in 2019, so its
current registration history starts then" (folder `…-planted`, answer key in
`planted.json`). `check` on the copy (bundle `3a84c7d14c49e873…`): **C1
undermined**, *contradicted (via C1 → A3): R1 line 8 `creationDate`: the record
gives 2007-10-09 where A3 says 2019; R2 line 8 …; and 10 more record lines
disagree the same way*. That is 12 lines across all three records: R1, ten
historical snapshots in R2, and the WHOIS text inside R3. A3 is the only
contradicted finding, and C2 and C3 are unchanged. No credits were used after
the lookups.

### Re-run with the model once OpenRouter is configured

Add `OPENROUTER_API_KEY` and `EVIDENCE_REVIEW_MODEL` to the root `.env` (the
launcher loads it as `./traced` does) or export them. Then, from the EDTH root:

```sh
# Model checks on the recorded runs, written next to the deterministic reports.
./traced-investigate check 20261107T131420Z-maltego-com-who-is-behind-maltego-com --reasoning openrouter \
  --out evidence-explorer/data/evidence-trail/investigations/20261107T131420Z-maltego-com-who-is-behind-maltego-com/check-openrouter
./traced-investigate check 20261107T131420Z-maltego-com-who-is-behind-maltego-com-planted --reasoning openrouter \
  --out evidence-explorer/data/evidence-trail/investigations/20261107T131420Z-maltego-com-who-is-behind-maltego-com-planted/check-openrouter

# A model-written draft over the same frozen records (no Maltego credits), checked with the model.
./traced-investigate run "Who is behind maltego.com?" --type maltego.Domain --value maltego.com \
  --record a3b685cdfeeab4fa2d22fa03cee68f5ecf35a432f3a0dc435d3ce5955185d8f2 \
  --record f3f0fd0e8c746520285d85e38e1f2747715def3d10ddd11075eb6aa796550700 \
  --record 67edbc9e08cf7c51301c15758e737e073d11f7b39eea142e07b48bad34260d81 \
  --reasoning openrouter
# Then plant into the claim that states the registration, as named in the new report:
./traced-investigate plant <new folder> --into <claim> \
  --statement "maltego.com was first registered in 2019, so its current registration history starts then."
./traced-investigate check <new folder>-planted --reasoning openrouter
```

## Limits and meaning

- A label is a machine check of traceability and consistency against the saved
  records. It establishes no truth, ownership, control, or intent, and it is not
  a human Use/Qualify/Withhold decision. Exact quotes establish traceability
  only.
- The deterministic layer is heuristic. Known gaps: names, countries, and free
  text are never compared for conflict; a statement with no typed value or
  property word stays `unchecked` without the model; and when some record line
  agrees, a value that differs in another snapshot is only a caveat. So "last
  updated in 2025" is not flagged if any snapshot says 2025. A
  `property: value` line can still produce a conflict that a person would
  dismiss. The report shows the exact line so a reviewer can decide.
- Records show what the Data MCP returned at one time. Two capabilities that
  draw on the same upstream source (for example two WhoisXML transforms) are not
  independent corroboration, and the checker does not treat them as such.
- Model output (the draft and any objection) is a proposal. Rejected quotes and
  discarded objections stay visible in the reports.

## Coordination handoff

The engine lives in `evidence_explorer/investigation/`; `tools/investigation/`
keeps compatible imports and the CLI. The viewer mounts intake on Cases and
reasoning checks in the existing Review slot, with shared record navigation,
review decisions and exports. It adds no schema, worker registration or model
transport. Each manual draft appends a new immutable run; earlier evidence and
reviews remain attached to their original snapshots. Browser reports remain
bound to the exact case, run and assessment that was checked.

Saved investigation folders stay under the explicitly selected workspace's
`investigations/` directory, alongside the existing Maltego record store. The
Maltego source dependency and command smoke checks are included in the same PR.
The public branch also incorporates the separately released Supabase source.

Live lookup forms and queued investigator/model drafting in the viewer remain
future work under the existing collection and worker owners. They should reuse
the current source services and model transport. The CLI remains available for
explicit lookups and model drafting. Operational runtime refresh and qualified
human validation remain separate from isolated code/browser verification.
