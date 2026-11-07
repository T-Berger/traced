# Traced interface guide

All visible apps use the selected [Traced / Branch Clock identity](../brand/traced/IDENTITY.txt). The canonical artwork, role tokens and local font licences live in `brand/traced/`. Use the supplied outlined logo; keep specialist names as descriptive subtitles. Package names and evidence identifiers retain their existing meanings.

## One visual system

Use Manrope for interface text and IBM Plex Mono for identifiers and timestamps. Reading surfaces use Paper and White with Ink text and Slate secondary text. Use Moss for underlined links. Primary actions use Vermilion with **Ink text**. Essential input boundaries use Slate; Line is a decorative divider. Focus uses a visible Ink ring on light surfaces and Paper on dark surfaces.

Use the semantic success, review, error and information roles with visible words. Brand orange indicates an action or the logo endpoint; it is not a truth, confidence or risk rating. Preserve Observation, Inference, Assumption, model proposal, specialist output and human decision as distinct concepts.

Dark review surfaces map the canonical Ink/Paper/link-on-dark roles to the existing application theme. Semantic dark tints are application accessibility mappings, not new identity masters. Check both colour schemes when changing these mappings. Keep the graph on its labelled light reading surface so its text, edges and exact-evidence outline remain readable in either scheme.

## Reuse and workflow

Shared review components own Cases → Review → Compare → Briefing; retain the existing route IDs. Case adapters supply content through existing slots. Reuse controls, source inspection, decisions, graph outlines, notices and export actions instead of introducing another theme or parallel review form. Give each screen a clear heading, short explanation and one primary next action. Keep advanced controls behind explicit disclosures, retain visible status and error recovery, and preserve user notes during navigation and refresh.

Describe the journey as **Plan investigation → Gather evidence → Confirm scope → Investigate findings → Review conclusions → Prepare and share briefing**. On Cases, make **Start investigation** a visible entry to the existing **Investigation plan** editor. Opening it performs no writes or collection and preserves working drafts. The primary purpose belongs to the saved plan; **Explore workflows** is optional session-local guidance. Use **Investigation plan** for the input and **Briefing** for the output, while preserving `MonitoringBrief` and other internal identifiers.

Use plain action labels without combining their effects: **Save plan** saves an immutable revision; **Attach collected evidence** associates the saved revision with the selected saved case; **Confirm scope** validates and records local confirmation. Explain that the attachment also supports imported evidence. Collect or import and save a case before attaching and confirming it; a plan can remain an unbound draft beforehand. Scope confirmation never approves findings.

Group intake under the **Gather evidence** disclosure. Carry forward only the exact local unbound plan revision the analyst explicitly saved or opened in the browser session. After another saved case is selected, **Review gathered evidence for your plan** opens a continuation showing the plan question and case name. **Use this case in saved plan** opens that revision and copies the frozen source references into working fields; it does not save, attach, or confirm. The selected case's loaded or dirty draft takes priority. Retain **Continue a saved plan → Open saved plan → Use this case's sources in the plan** as the manual path. A new plan with an older case selected normally leads to **Gather evidence**, while explicit use of that case remains available.

Show a compact **Plan → Evidence → Scope → Findings** guide using factual state labels, not inferred completion. Direct next actions to the relevant control without executing its operation. After confirmation, **Open first finding** selects the first undecided finding; collection-count findings keep **Inspect collected material**, and a case without findings keeps its existing evidence-inspection path. When all findings have current decisions, offer the briefing preview. Keep **Start another plan** secondary to the main entry action. Put exact revisions, **Adopt selected version for this case**, and **Copy fields into a separate plan** under **Plan versions and imported history**; selecting a saved plan or version alone does not replace working fields.

Optional analysis belongs behind a clear disclosure with its prerequisites and current state visible. Collection alone creates no substantive claim assessment. Keep scheduled collection, manual saved-window activity comparison, and the absence of a live alert service explicit. Unavailable analysis must leave evidence inspection reachable and explain the recovery path without manufacturing missing coverage.

Keep exact source text, source origin, current workspace, assessment and reviewer decision visible where they affect interpretation. Mixed real and fictional evidence keeps explicit labels through export. Comparison candidates are not independent corroboration. Unknown original context stays unknown.

An available next action is a navigation recommendation, separate from process completion. Keep unconfirmed scope visible while offering useful destinations supported by the selected saved case and assessment. Reuse the shared context-bound navigation helper and its existing page writer; do not add competing routing callbacks. Actions on other panels need a unique `location` so their component IDs do not collide with the guidance bar. Preserve unsaved drafts and validate the current case, assessment version, run and exact targets again on click.

Graphs show a bounded, informative neighbourhood. Keep readable labels, consistent arrow direction, shown/total coverage, exact edge details and an accessible relationship list. Narrow layouts use the full text relationship list instead of shrinking the canvas. Do not add dense edge labels or hide exclusions and coverage to make a graph look cleaner.

Media starts hidden without an image URL or request. Reveal and Retry use the same saved record and expected hash; a closed disclosure alone does not defer loading. Deliver the exact bounded bytes that passed hash validation, and include the expected saved hash in viewer requests. Clear revealed media when its case, assessment or record changes. Replace missing, changed or unreadable images with labelled recovery and access to saved metadata. Preserve draft text and review decisions throughout these display actions.

## Shared assets and ongoing checks

`ui/traced-base.css` supplies shared logo, focus and motion behaviour. Each app has a scoped `zz_traced_theme.css` bridge for its existing selectors. Canonical assets are copied into each installable package; runtime assets never depend on the repository root or remote fonts.

After an approved identity/base-rule change, run `python3 tools/sync_traced_identity.py`. Run `make identity-check` to catch missing or changed copies; this also runs in `make check`. Edit the masters and sync, rather than editing generated token/font/logo copies. Feature CSS uses the role tokens, not a fresh palette.

For a visual change, inspect the affected app at desktop and 390 px width; verify local logo/font/favicon loading, long labels, focus, input boundaries, primary/disabled actions, notices and dark mode when supported. Preserve meaningful existing tests and exercise the relevant review → decision → briefing → export/reopen flow. Keep automated checks separate from qualified human review. Handoffs name the files, actual checked surfaces, artifacts and remaining limits.
